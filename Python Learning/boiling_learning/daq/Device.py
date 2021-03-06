from nidaqmx.task import Task

from boiling_learning.utils.utils import (
    SimpleRepr,
    SimpleStr,
    DictEq
)

class Device(SimpleRepr, SimpleStr, DictEq):
    def __init__(self, name: str = ''):
        self.name = name

    @property
    def path(self) -> str:
        return self.name

    def exists(self, task: Task) -> bool:
        return self.path in set(device.name for device in task.devices)