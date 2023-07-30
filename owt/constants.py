from typing import Literal

AudioCodec = Literal['pcmu', 'pcma', 'opus', 'g722', 'iSAC', 'iLBC', 'aac', 'ac3', 'nellymoser']
VideoCodec = Literal['vp8', 'vp9', 'h264', 'h265', 'av1', 'av1x']
VideoProfile = Literal['B', 'CB', 'M', 'E', 'H']

AudioSourceInfo = Literal['mic', 'screen-cast', 'raw-file', 'encoded-file', 'mixed']
"""
Source info about an audio track. Values: 'mic', 'screen-cast',
'file', 'mixed'.
"""

VideoSourceInfo = Literal['camera', 'screen-cast', 'raw-file', 'encoded-file', 'mixed']
"""
Source info about a video track. Values: 'camera', 'screen-cast',
'file', 'mixed'.
"""

TransportType = Literal['quic', 'webrtc']