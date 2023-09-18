import json
from os import path as Path

import pytest

from owt import ConferenceClient, ConferenceInfo, create_local_tracks, LocalStream, RemoteStream
from owt.utils import create_token_from_owt_demo


from .types import TestConfig


@pytest.fixture
def config() -> TestConfig:
    server_path = Path.normpath(Path.join(Path.dirname(__file__), 'fixtures', 'config.json'))
    with open(server_path, 'r') as f:
        config: TestConfig = json.load(f)
        return config
    
@pytest.fixture
async def token(config: TestConfig) -> str:
    server = config.get('server')
    assert server
    url = server.get('url')
    assert url
    user_name = server.get('user_name')
    assert user_name
    role = server.get('role')
    assert role
    room = server.get('room') or ''
    return await create_token_from_owt_demo(url, user_name, role, room=room)

@pytest.fixture
def client(config: TestConfig) -> ConferenceClient:
    rtc_config = config['server'].get('rtc_config') or {}
    return ConferenceClient(config={ "rtcConfiguration": rtc_config })

@pytest.fixture
async def conference(client: ConferenceClient, token: str):
    try:
        conference = await client.join(token)
        yield conference
    finally:
        await client.leave()

@pytest.mark.asyncio
async def test_join_and_leave(client: ConferenceClient, conference: ConferenceInfo, config: TestConfig):
    await client.wait_joined()
    assert client.joined()
    assert conference.id
    assert conference.self
    assert conference.self.userId == config['server']['user_name']
    assert conference.self.role == config['server']['role']

@pytest.fixture
def local_stream():
    stream = create_local_tracks('file', Path.join(Path.dirname(__file__), 'fixtures/video.mp4'))
    stream.attributes = {
        'tag': 'test-video',
        'origin': stream.id,
    }
    return stream

@pytest.mark.asyncio
async def test_publish(client: ConferenceClient, conference: ConferenceInfo, local_stream: LocalStream):
    pub = await client.publish(local_stream)
    remote_stream = await client.wait_for_stream(lambda st: st.attributes.get('origin') == local_stream.id, timeout=10)
    assert remote_stream
    assert pub.id
    
@pytest.mark.asyncio
async def test_subscibe(client: ConferenceClient, conference: ConferenceInfo, local_stream: LocalStream):
    await client.publish(local_stream)
    remote_stream = await client.wait_for_stream(lambda st: st.attributes.get('origin') == local_stream.id, timeout=10)
    sub = await client.subscribe(remote_stream)
    assert sub.video_track

@pytest.mark.asyncio
async def test_consume_stream(client: ConferenceClient, conference: ConferenceInfo, local_stream: LocalStream):
    await client.publish(local_stream)
    async def consume_stream(stream: RemoteStream):
        sub = await client.subscribe(stream)
        track = sub.video_track()
        assert track
        i = 0
        while i < 10:
            await track.recv()
            i += 1
        
    handle = client.consume_streams(consume_stream, timeout=10)
    await handle.wait()
    

