from typing import TypedDict

class TestServerConfig(TypedDict):
    url: str
    user_name: str
    role: str
    room: str

class TestConfig(TypedDict):
    server: TestServerConfig