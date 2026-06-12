from __future__ import annotations
from meta import MathObject
from typing import Annotated
from fractions import Fraction

class Concept(MathObject):
    name: Annotated[str,lambda x :x.islower()]

x1 = Concept("x")
x2 = Concept("X")
