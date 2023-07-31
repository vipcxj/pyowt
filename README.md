# pyowt

[![PyPI](https://img.shields.io/pypi/v/owt.svg)](https://pypi.python.org/pypi)
![PyPI - Downloads](https://img.shields.io/pypi/dm/owt)

A python client for [Intel OWT Server](https://github.com/open-webrtc-toolkit/owt-server)

## Installation
You can install using `pip`:

```bash
pip install owt
```

## Quick Start

```python
from owt import ConferenceClient

# create a client
client = ConferenceClient()
try:
    # join the conference using token created by owt-server
    # or if your owt-server is using the demo, the package provide an 
    # api to request token from the server directly. (It should not be permit in product environment)
    # from owt.utils import create_token_from_owt_demo
    # token = await create_token_from_owt_demo(url, user_name, role, room=room)
    conference = await client.join(token)
    # To publish, you need a LocalStream. 
    # To create a localStream, you need a MediaStreamTrack
    # which is the class of package aiortc. 
    # So you can find document of how to create a MediaStreamTrack
    # from https://github.com/aiortc/aiortc.
    # Here I provide a simple way to create it:
    stream = create_local_tracks('file', Path.join(Path.dirname(__file__), 'fixtures/video.mp4'))
    # You can add some custom attribute to the stream. After published, 
    # another endpoint will accept it and using it to distinguish the stream.
    stream.attributes = {
        'tag': 'test-video',
        'origin': stream.id,
    }
    # now publish it.
    pub = await client.publish(stream)
    # Though after the method publish returned, the publication is completed. 
    # you may not accept the new remote stream 
    # (Of course you can subscribe the stream you published)
    # immediately. Here I provide an api to wait it.
    remote_stream = await client.wait_for_stream(lambda st: st.attributes.get('origin') == local_stream.id, timeout=10)
    # now you can subscribe the video you just published
    sub = await client.subscribe(remote_stream)
    # You can read the video stream from sub.video_track()
    # or audio stream from sub.audio_track() 
    track = sub.video_track()
    # We know we just publish a video, so the video track should not be None
    assert track
    while True:
        # What's is frame is? Please see the document of [aiortc](https://github.com/aiortc/aiortc), Of course, you can convert it to numpy array.
        frame = await track.recv()
        # do something about the frame
    await sub.close()
    await pub.close()
finally:
    await client.leave()
```
This package is based on [aiortc](https://github.com/aiortc/aiortc) because it need the support of webrtc tec. And [aiortc](https://github.com/aiortc/aiortc) is based on [pyav](https://github.com/PyAV-Org/PyAV). So I suggest you read the document of them too.

Here is another useful api:
```python
from owt import ConferenceClient

client = ConferenceClient()
try:
    await client.join(token)
    async def consume_stream(stream: RemoteStream):
        # first filter the stream
        if ...:
            # then subscribe it
            sub = await client.subscribe(stream)
            track = sub.video_track()
            # then process it
            while True:
                frame = await track.recv()
                ...
    # The method consume_streams will consume all the remote streams 
    # (Someone published, here someone include yourselvies.), 
    # including the stream exist and coming in in future.
    # Here timeout mean the method will wait 10 second until all the 
    # consume_stream callbacks have done and no new remote streams found.
    handle = client.consume_streams(consume_stream, timeout=10)
    # The handle will return only when all your consume_stream callbacks
    # have done and no new remote streams found for 10 seconds.
    await handle.wait()
finally:
    # leave the conference will automatically close all the 
    # related publications and subscribations
    await client.leave()
```