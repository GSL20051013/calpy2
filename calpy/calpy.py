from __future__ import annotations
from meta import MathObject
from typing import Annotated
from fractions import Fraction

class Concept(MathObject):
    name:str

    def __with__(self):
        return self.clone()

x1 = Concept('x')
x2 = Concept('x')

with x1 as x:
    x.name = 'y'