from sandloom.sdk import contract


@contract("a.b_c.v1")
class BC:
    async def ping(self) -> int:
        return 1
