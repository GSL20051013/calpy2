import sys
import types
from functools import singledispatchmethod
from typing import Any

if sys.version_info >= (3, 11):
    from typing import dataclass_transform
else:
    from typing_extensions import dataclass_transform

# A comprehensive set of math dunder methods that should support singledispatch
MATH_DUNDERS = {
    # Binary operators
    '__add__', '__sub__', '__mul__', '__matmul__', '__truediv__', '__floordiv__',
    '__mod__', '__divmod__', '__pow__', '__lshift__', '__rshift__', '__and__', '__xor__', '__or__',
    # Reflected binary operators
    '__radd__', '__rsub__', '__rmul__', '__rmatmul__', '__rtruediv__', '__rfloordiv__',
    '__rmod__', '__rdivmod__', '__rpow__', '__rlshift__', '__rrshift__', '__rand__', '__rxor__', '__ror__',
    # Augmented assignment
    '__iadd__', '__isub__', '__imul__', '__imatmul__', '__itruediv__', '__ifloordiv__',
    '__imod__', '__ipow__', '__ilshift__', '__irshift__', '__iand__', '__ixor__', '__ior__'
}

class MathNamespace(dict):
    """
    Custom dictionary for the class body namespace. Intercepts definitions of 
    math dunder methods and wraps them in singledispatchmethod automatically.
    """
    def __setitem__(self, key, value):
        # Only intercept predefined math dunders that are normal functions
        if key in MATH_DUNDERS and isinstance(value, types.FunctionType):
            # If the dunder is already a singledispatchmethod, register the new
            # function to it rather than overwriting it (allows implicit overloading).
            if key in self and isinstance(self[key], singledispatchmethod):
                self[key].register(value)
                return
            
            # Otherwise, wrap the first definition in singledispatchmethod
            value = singledispatchmethod(value)
            
        super().__setitem__(key, value)


@dataclass_transform(eq_default=True)
class MathMeta(type):
    
    @classmethod
    def __prepare__(mcs, name, bases, **kwargs):
        # Inject our custom namespace handler during class body execution
        return MathNamespace()

    def __new__(mcs, name, bases, namespace):
        annotations = namespace.get('__annotations__', {})
        current_fields = list(annotations.keys())
        current_defaults = {}

        if '__slots__' not in namespace:
            namespace['__slots__'] = tuple(current_fields)

        for f in current_fields:
            if f in namespace:
                current_defaults[f] = namespace.pop(f)

        all_fields = []
        all_defaults = {}

        for base in bases:
            if isinstance(base, MathMeta):
                for f in getattr(base, '_meta_fields', []):
                    if f not in all_fields:
                        all_fields.append(f)
                all_defaults.update(getattr(base, '_meta_defaults', {}))

        for f in current_fields:
            if f not in all_fields:
                all_fields.append(f)
        all_defaults.update(current_defaults)

        # Convert back to a standard dict to prevent edge cases with type.__new__
        cls = super().__new__(mcs, name, bases, dict(namespace))

        setattr(cls, '_meta_fields', all_fields)
        setattr(cls, '_meta_defaults', all_defaults)

        if not all_fields:
            return cls

        exec_globals = {}
        init_args = ["self"]
        init_body = []

        for f in all_fields:
            if f in all_defaults:
                exec_globals[f"def_{f}"] = all_defaults[f]
                init_args.append(f"{f}=def_{f}")
            else:
                init_args.append(f)
            
            init_body.append(f"    self.{f} = {f}")

        init_args.extend(["*args", "**kwargs"])

        if hasattr(cls, '__post_init__'):
            init_body.append("    self.__post_init__(*args, **kwargs)")

        init_func_str = f"def __init__({', '.join(init_args)}):\n" + "\n".join(init_body)
        
        exec_locals = {}
        exec(init_func_str, exec_globals, exec_locals) 
        setattr(cls, '__init__', exec_locals['__init__'])

        self_tuple = ", ".join(f"self.{f}" for f in all_fields) + ("," if len(all_fields) == 1 else "")
        other_tuple = ", ".join(f"other.{f}" for f in all_fields) + ("," if len(all_fields) == 1 else "")
        
        eq_func_str = (
            f"def __eq__(self, other):\n"
            f"    if self.__class__ is not getattr(other, '__class__', None):\n"
            f"        return NotImplemented\n"
            f"    return ({self_tuple}) == ({other_tuple})\n"
        )
        
        exec(eq_func_str, exec_globals, exec_locals)
        setattr(cls, '__eq__', exec_locals['__eq__'])

        return cls


class MathObject(metaclass=MathMeta):

    def __post_init__(self, *args: Any, **kwargs: Any) -> None:
        pass