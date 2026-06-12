from meta import MathObject

class Concept(MathObject):
    name:str

    def __deepcopy__(self,memo):
        return self

class Variable(Concept):
    pass

class Function(Concept):
    signature:tuple