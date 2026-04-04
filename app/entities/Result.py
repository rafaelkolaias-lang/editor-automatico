from typing import TypeVar, Generic

T = TypeVar('data')

class Result(Generic[T]):
  success: bool
  data: T
  error: str

  def __init__(self, success: bool, data: T = None, error: str = ''):
    self.success = success
    self.data = data
    self.error = error
