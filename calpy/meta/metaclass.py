import copy
from functools import wraps
import inspect
import sys
import types
import weakref
from fractions import Fraction
from typing import Annotated, Any, ClassVar, get_args, get_origin, get_type_hints

if sys.version_info >= (3, 11):
    from typing import dataclass_transform
else:
    from typing_extensions import dataclass_transform

_OVERLOADS_KEY = "__cy_overloads__"
_QUANTUM_KEY = "__cy_quantum_methods__"
_MISSING = object()
# CPython refcount in this write path includes several temporary stack refs;
# threshold 4 still corresponds to one effective external owner.
_UNIQUE_REFCOUNT_THRESHOLD = 4
_IMMUTABLE_CLONE_TYPES = (int, float, bool, str, bytes, tuple, frozenset, type(None), Fraction)

_ALGEBRAIC_FALLBACKS = {
    "__add__": lambda self, other: other + self,
    "__radd__": lambda self, other: self + other,
    "__sub__": lambda self, other: self + (-other),
    "__rsub__": lambda self, other: (-self) + other,
    "__mul__": lambda self, other: other * self,
    "__rmul__": lambda self, other: self * other,
    "__truediv__": lambda self, other: self * (1 / other),
    "__rtruediv__": lambda self, other: (self.__inverse__() * other) if hasattr(self, "__inverse__") else NotImplemented,
}


class MathNamespace(dict):
    def __setitem__(self, key, value):
        if key in (_OVERLOADS_KEY, _QUANTUM_KEY):
            super().__setitem__(key, value)
            return

        overloads = self.get(_OVERLOADS_KEY)
        if overloads is None:
            overloads = {}
            super().__setitem__(_OVERLOADS_KEY, overloads)

        quantum = self.get(_QUANTUM_KEY)
        if quantum is None:
            quantum = {}
            super().__setitem__(_QUANTUM_KEY, quantum)

        existing = self.get(key, None)
        if existing is ... and isinstance(value, types.FunctionType):
            quantum[key] = value
            return
        if isinstance(existing, types.FunctionType) and value is ...:
            quantum[key] = existing
            super().__setitem__(key, value)
            return

        if isinstance(value, types.FunctionType) and isinstance(existing, types.FunctionType):
            overloads.setdefault(key, [existing]).append(value)
            return

        if isinstance(value, types.FunctionType) and key in overloads:
            overloads[key].append(value)
            return

        super().__setitem__(key, value)


def _resolve_type(annotation: Any, cls: type) -> Any:
    if annotation in (inspect.Signature.empty, Any, object):
        return Any
    if annotation == "Self" or annotation == cls.__name__:
        return cls
    return annotation


def _matches_annotation(value: Any, annotation: Any) -> bool:
    if annotation is Any:
        return True
    origin = get_origin(annotation)
    if origin is Annotated:
        annotation = get_args(annotation)[0]
        origin = get_origin(annotation)
    if origin in (types.UnionType, getattr(sys.modules.get("typing"), "Union", None)):
        return any(_matches_annotation(value, arg) for arg in get_args(annotation))
    if isinstance(annotation, type):
        return isinstance(value, annotation)
    return True


def _coerce_value(value: Any, annotation: Any) -> Any:
    # Preserve nullable behavior: explicit None is accepted without coercion.
    if value is None:
        return value
    if annotation in (Any, object, inspect.Signature.empty):
        return value
    origin = get_origin(annotation)
    if origin is Annotated:
        annotation = get_args(annotation)[0]
        origin = get_origin(annotation)
    if origin in (types.UnionType, getattr(sys.modules.get("typing"), "Union", None)):
        for candidate in get_args(annotation):
            try:
                return _coerce_value(value, candidate)
            except (TypeError, ValueError):
                continue
        return value
    # Requested precision upgrade path: int/float -> Fraction.
    if annotation is Fraction and isinstance(value, (int, float)):
        return Fraction(value)
    if isinstance(annotation, type) and not isinstance(value, annotation):
        try:
            return annotation(value)
        except Exception:
            return value
    return value


def _build_dispatcher(name: str, overloads: list[types.FunctionType], cls: type):
    signatures = [(fn, inspect.signature(fn)) for fn in overloads]
    resolved_hints = {}
    for fn, _ in signatures:
        try:
            resolved_hints[fn] = get_type_hints(
                fn,
                globalns=vars(sys.modules[fn.__module__]),
                localns={cls.__name__: cls, "Self": cls},
                include_extras=True,
            )
        except (NameError, TypeError, AttributeError):
            resolved_hints[fn] = {}

    def dispatcher(self, *args, **kwargs):
        for fn, sig in signatures:
            try:
                bound = sig.bind(self, *args, **kwargs)
            except TypeError:
                continue
            ok = True
            for param_name, param in sig.parameters.items():
                if param_name == "self" or param_name not in bound.arguments:
                    continue
                annotation = resolved_hints[fn].get(param_name, param.annotation)
                annotation = _resolve_type(annotation, cls)
                if not _matches_annotation(bound.arguments[param_name], annotation):
                    ok = False
                    break
            if ok:
                return fn(*bound.args, **bound.kwargs)

        if name in _ALGEBRAIC_FALLBACKS and len(args) == 1 and not kwargs:
            try:
                return _ALGEBRAIC_FALLBACKS[name](self, args[0])
            except (TypeError, AttributeError):
                return NotImplemented
        return NotImplemented

    dispatcher.__name__ = name
    return dispatcher


def _install_algebraic_fallbacks(cls: type) -> None:
    if "__add__" in cls.__dict__ and "__radd__" not in cls.__dict__:
        def __radd__(self, other):
            try:
                other_add = getattr(other, "__add__", None)
                if callable(other_add):
                    result = other_add(self)
                    if result is not NotImplemented:
                        return result
                return self.__add__(other)
            except (TypeError, AttributeError):
                return NotImplemented

        setattr(cls, "__radd__", __radd__)

    if "__add__" in cls.__dict__ and "__sub__" not in cls.__dict__:
        def __sub__(self, other):
            try:
                return self + (-other)
            except (TypeError, AttributeError):
                return NotImplemented

        setattr(cls, "__sub__", __sub__)

    if "__add__" in cls.__dict__ and "__rsub__" not in cls.__dict__:
        def __rsub__(self, other):
            try:
                return (-self) + other
            except (TypeError, AttributeError):
                return NotImplemented

        setattr(cls, "__rsub__", __rsub__)

    if "__mul__" in cls.__dict__ and "__rmul__" not in cls.__dict__:
        def __rmul__(self, other):
            try:
                return self.__mul__(other)
            except (TypeError, AttributeError):
                return NotImplemented

        setattr(cls, "__rmul__", __rmul__)

    if "__inverse__" in cls.__dict__ and "__truediv__" not in cls.__dict__:
        def __truediv__(self, other):
            if not hasattr(other, "__inverse__"):
                return NotImplemented
            try:
                return self * other.__inverse__()
            except (TypeError, AttributeError):
                return NotImplemented

        setattr(cls, "__truediv__", __truediv__)

    if "__inverse__" in cls.__dict__ and "__rtruediv__" not in cls.__dict__:
        def __rtruediv__(self, other):
            try:
                return other * self.__inverse__()
            except (TypeError, AttributeError):
                return NotImplemented

        setattr(cls, "__rtruediv__", __rtruediv__)


@dataclass_transform(eq_default=True)
class MathMeta(type):
    _flyweight_pool: weakref.WeakValueDictionary

    @classmethod
    def __prepare__(mcs, name, bases, **kwargs):
        return MathNamespace()

    def __new__(mcs, name, bases, namespace):
        raw_namespace = dict(namespace)
        overloads = raw_namespace.pop(_OVERLOADS_KEY, {})
        quantum_methods = raw_namespace.pop(_QUANTUM_KEY, {})

        annotations = raw_namespace.get("__annotations__", {})
        current_fields = []
        current_types = {}
        current_bounds = {}
        current_defaults = {}

        for field, annotation in annotations.items():
            if get_origin(annotation) is ClassVar:
                continue
            current_fields.append(field)
            if get_origin(annotation) is Annotated:
                args = get_args(annotation)
                current_types[field] = args[0]
                current_bounds[field] = [meta for meta in args[1:] if callable(meta)]
            else:
                current_types[field] = annotation

            if field in raw_namespace:
                current_defaults[field] = raw_namespace.pop(field)

        for qfield in quantum_methods:
            if qfield not in current_fields:
                current_fields.append(qfield)
                current_types[qfield] = Any
                current_defaults[qfield] = ...
            if qfield in raw_namespace:
                raw_namespace.pop(qfield)

        base_fields = []
        base_types = {}
        base_defaults = {}
        base_bounds = {}
        base_quantum = {}
        for base in bases:
            if isinstance(base, MathMeta):
                for field in getattr(base, "_meta_fields", ()):
                    if field not in base_fields:
                        base_fields.append(field)
                base_types.update(getattr(base, "_meta_types", {}))
                base_defaults.update(getattr(base, "_meta_defaults", {}))
                base_bounds.update(getattr(base, "_meta_bounds", {}))
                base_quantum.update(getattr(base, "_meta_quantum", {}))

        all_fields = list(base_fields)
        for field in current_fields:
            if field not in all_fields:
                all_fields.append(field)

        all_types = {**base_types, **current_types}
        all_defaults = {**base_defaults, **current_defaults}
        all_bounds = {**base_bounds, **current_bounds}
        all_bounds.update(raw_namespace.pop("__bounds__", {}))

        inherited = set(base_fields)
        class_slots = [field for field in current_fields if field not in inherited]
        if "__slots__" not in raw_namespace:
            slots = list(class_slots)
            if not any(isinstance(base, MathMeta) for base in bases):
                slots.append("__weakref__")
            slots = tuple(slots)
            raw_namespace["__slots__"] = slots

        cls = super().__new__(mcs, name, bases, raw_namespace)
        cls._meta_fields = all_fields
        cls._meta_types = all_types
        cls._meta_defaults = all_defaults
        cls._meta_bounds = all_bounds
        cls._meta_quantum = {**base_quantum, **quantum_methods}
        cls._flyweight_pool = weakref.WeakValueDictionary()
        cls.__match_args__ = tuple(all_fields)

        type_hints = {}
        try:
            type_hints = get_type_hints(cls, include_extras=True)
        except (NameError, TypeError, AttributeError):
            type_hints = dict(all_types)
        cls._meta_types = {**all_types, **{k: v for k, v in type_hints.items() if k in all_fields}}

        for method_name, method_overloads in overloads.items():
            if method_name in raw_namespace and isinstance(raw_namespace[method_name], types.FunctionType):
                method_overloads = [raw_namespace[method_name], *method_overloads[1:]]
            setattr(cls, method_name, _build_dispatcher(method_name, method_overloads, cls))

        def __init__(self, *args, **kwargs):
            values = {}
            field_count = len(self._meta_fields)
            positional = args[:field_count]
            post_args = args[field_count:]

            for idx, field in enumerate(self._meta_fields):
                if idx < len(positional):
                    values[field] = positional[idx]
                elif field in kwargs:
                    values[field] = kwargs.pop(field)
                elif field in self._meta_defaults:
                    values[field] = self._meta_defaults[field]
                else:
                    raise TypeError(f"Missing required argument: {field}")

            if self._meta_quantum:
                for qfield in self._meta_quantum:
                    values.setdefault(qfield, ...)

            for field, value in values.items():
                setattr(self, field, value)

            if hasattr(self, "__post_init__"):
                self.__post_init__(*post_args, **kwargs)
            elif kwargs:
                unknown = ", ".join(kwargs.keys())
                raise TypeError(f"Unexpected keyword arguments: {unknown}")

        setattr(cls, "__init__", __init__)
        init_params = []
        seen_default = False
        for field in cls._meta_fields:
            annotation = cls._meta_types.get(field, inspect.Signature.empty)
            has_default = field in cls._meta_defaults
            default = cls._meta_defaults[field] if has_default else inspect.Parameter.empty
            kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
            if seen_default and not has_default:
                kind = inspect.Parameter.KEYWORD_ONLY
            init_params.append(
                inspect.Parameter(
                    field,
                    kind,
                    default=default,
                    annotation=annotation,
                )
            )
            if has_default:
                seen_default = True
        init_params.extend(
            (
                inspect.Parameter("_post_args", inspect.Parameter.VAR_POSITIONAL),
                inspect.Parameter("_post_kwargs", inspect.Parameter.VAR_KEYWORD),
            )
        )
        init_signature = inspect.Signature(parameters=init_params)
        __init__.__signature__ = init_signature
        cls.__signature__ = init_signature

        def __repr__(self):
            parts = ", ".join(f"{f}={getattr(self, f)!r}" for f in self._meta_fields if hasattr(self, f))
            return f"{self.__class__.__name__}({parts})"

        def __iter__(self):
            for field in self._meta_fields:
                yield getattr(self, field)

        def __eq__(self, other):
            if self.__class__ is not getattr(other, "__class__", None):
                return NotImplemented
            return tuple(getattr(self, f) for f in self._meta_fields) == tuple(
                getattr(other, f) for f in self._meta_fields
            )

        def __hash__(self):
            return hash(tuple(getattr(self, f) for f in self._meta_fields))

        def clone(self):
            new_obj = object.__new__(self.__class__)
            for field in self._meta_fields:
                field_value = getattr(self, field)
                if isinstance(field_value, _IMMUTABLE_CLONE_TYPES):
                    object.__setattr__(new_obj, field, field_value)
                else:
                    object.__setattr__(new_obj, field, copy.deepcopy(field_value))
            return new_obj

        setattr(cls, "__repr__", __repr__)
        setattr(cls, "__iter__", __iter__)
        setattr(cls, "__eq__", __eq__)
        setattr(cls, "__hash__", __hash__)
        setattr(cls, "clone", clone)

        if "__with__" in cls.__dict__:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                handler = self.__with__
                param_count = len(inspect.signature(handler).parameters)
                if param_count >= 3:
                    handler(exc_type, exc, tb)
                else:
                    handler()
                return False

            setattr(cls, "__enter__", __enter__)
            setattr(cls, "__exit__", __exit__)

        _install_algebraic_fallbacks(cls)

        for attr_name, attr_value in list(cls.__dict__.items()):
            if attr_name.startswith("__") or attr_name in {"clone"}:
                continue
            if isinstance(attr_value, (staticmethod, classmethod, property)):
                continue
            if not callable(attr_value):
                continue

            def _make_chain_wrapper(fn):
                @wraps(fn)
                def _wrapped(self, *args, **kwargs):
                    result = fn(self, *args, **kwargs)
                    return self if result is None else result
                return _wrapped

            setattr(cls, attr_name, _make_chain_wrapper(attr_value))

        return cls

    def __call__(cls, *args, **kwargs):
        instance = super().__call__(*args, **kwargs)
        try:
            key = tuple(getattr(instance, field, _MISSING) for field in cls._meta_fields)
            pooled = cls._flyweight_pool.get(key)
            if pooled is not None:
                return pooled
            cls._flyweight_pool[key] = instance
        except (TypeError, AttributeError):
            pass
        return instance


class MathObject(metaclass=MathMeta):
    def __getattribute__(self, name: str):
        if name not in {"_meta_quantum", "_meta_fields", "_meta_types", "_meta_bounds"}:
            qmap = object.__getattribute__(self, "_meta_quantum")
            if name in qmap:
                try:
                    value = object.__getattribute__(self, name)
                except AttributeError:
                    value = ...
                if value is ...:
                    computed = qmap[name](self)
                    object.__setattr__(self, name, computed)
                    return computed

            get_hook = f"__get_{name}__"
            cls_dict = object.__getattribute__(self, "__class__").__dict__
            if get_hook in cls_dict:
                raw = object.__getattribute__(self, name)
                hooked = cls_dict[get_hook](self, raw)
                return raw if hooked is None else hooked
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in getattr(self, "_meta_types", {}):
            value = _coerce_value(value, self._meta_types[name])
            validators = self._meta_bounds.get(name, ())
            if callable(validators):
                validators = (validators,)
            for validator in validators:
                if not validator(value):
                    raise ValueError(f"Boundary failed for '{name}' with value {value!r}")
            # Ellipsis is the quantum placeholder value set during bootstrap.
            if value is not ... and name in getattr(self, "_meta_fields", ()):
                current = _MISSING
                try:
                    current = object.__getattribute__(self, name)
                except AttributeError:
                    current = _MISSING
                if current is not _MISSING and current != value and sys.getrefcount(self) > _UNIQUE_REFCOUNT_THRESHOLD:
                    raise RuntimeError(f"Shared instance mutation blocked for field '{name}'; call clone() before modifying fields.")

        object.__setattr__(self, name, value)

        set_hook_name = f"__set_{name}__"
        set_hook = getattr(type(self), set_hook_name, None)
        if set_hook is not None:
            set_hook(self, value)

        if hasattr(type(self), "_flyweight_pool"):
            try:
                key = tuple(getattr(self, field, _MISSING) for field in self._meta_fields)
                pool = type(self)._flyweight_pool
                if sys.getrefcount(self) <= _UNIQUE_REFCOUNT_THRESHOLD:
                    pool[key] = self
                else:
                    if pool.get(key) is self:
                        pool.pop(key, None)
            except (TypeError, AttributeError):
                pass

    def __post_init__(self, *args: Any, **kwargs: Any) -> None:
        pass