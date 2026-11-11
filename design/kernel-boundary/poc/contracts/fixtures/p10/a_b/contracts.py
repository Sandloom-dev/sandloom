from sandloom.sdk import contract


@contract("a_b.c.v1")
class C:
    async def ping(self, x: int) -> int:
        return x
