from dataclasses import dataclass
from enum import Enum
from typing import Callable


# 四个权限标签中，目前 Registry 只接受 READ 和 WRITE。
class Permission(str, Enum):
    READ = "READ"
    WRITE = "WRITE"
    EXECUTE = "EXECUTE"
    DANGEROUS = "DANGEROUS"


@dataclass(frozen=True)
# 一个工具的定义：名字、说明、参数类型、真正执行的函数、权限。
class Tool:
    name: str
    description: str
    parameters: dict[str, type]
    # handler 例如 files.read_file；模型只看到 schema，不会拿到 Python 函数。
    handler: Callable[..., str]
    permission: Permission = Permission.READ

    def schema(self) -> dict:
        # 将 Python 类型转换为模型 API 接受的 JSON Schema 类型名。
        types = {str: "string", int: "integer"}
        return {"name": self.name, "description": self.description, "input_schema": {
            "type": "object", "properties": {k: {"type": types[v]} for k, v in self.parameters.items()},
            "required": list(self.parameters), "additionalProperties": False}}
