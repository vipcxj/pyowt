import sys
if sys.version_info < (3, 11):
    from typing_extensions import NotRequired, TypedDict
else:
    from typing import NotRequired, TypedDict
    
from owt.client import RTCConfigurationDict

class TestServerConfig(TypedDict):
    url: str
    user_name: str
    role: str
    room: NotRequired[str]
    rtc_config: NotRequired[RTCConfigurationDict]

class TestConfig(TypedDict):
    server: TestServerConfig