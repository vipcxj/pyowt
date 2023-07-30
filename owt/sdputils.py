import logging
import re
from typing import Literal

from .publication import AudioEncodingParameters, VideoEncodingParameters

logger = logging.getLogger('OWT')

def findLineInRange(sdpLines: list[str], startLine: int, endLine: int, prefix: str, substr: str | None = None) -> int:
    realEndLine = endLine if endLine != -1 else len(sdpLines)
    for i in range(startLine, realEndLine):
        if sdpLines[i].startswith(prefix):
            if not substr or sdpLines[i].lower().find(substr.lower()) != -1:
                return i
    return -1

def findLine(sdpLines: list[str], prefix: str, substr: str | None = None) -> int:
    return findLineInRange(sdpLines, 0, -1, prefix, substr)

def findMLineRangeWithMID(sdpLines: list[str], mid: str) -> tuple[int, int] | tuple[None, None]:
    midLine = f'a=mid:{mid}'
    midIndex = findLine(sdpLines, midLine)
    if midIndex < 0:
        logging.debug('Specified MID is not found')
        return None, None
    while midIndex >= 0 and sdpLines[midIndex] != midLine:
        midIndex = findLineInRange(sdpLines, midIndex, -1, midLine)
    if midIndex >= 0:
        nextMLineIndex = findLineInRange(sdpLines, midIndex, -1, 'm=')
        mLineIndex = -1
        for i in reversed(range(midIndex)):
            if sdpLines[i].find('m=') >= 0:
                mLineIndex = i
                break
        if mLineIndex >= 0:
            return mLineIndex, nextMLineIndex
    return None, None

def getCodecPayloadTypeFromLine(sdpLine: str) -> str | None:
    m = re.search(r'a=rtpmap:(\d+) [a-zA-Z0-9-]+\/\d+', sdpLine)
    if m is not None:
        return m.group(1)
    else:
        return None

def getCodecPayloadType(sdpLines: list[str], codec: str) -> str | None:
    index = findLine(sdpLines, 'a=rtpmap', codec)
    if index != -1:
        return getCodecPayloadTypeFromLine(sdpLines[index])
    else:
        return None
    
def parseFmtpLine(fmtpLine: str) -> tuple[str, dict[str, str]] | tuple[None, None]:
    """
    Split an fmtp line into an tuple including 'pt' and 'params'.

    Args:
        fmtpLine (str): an fmtp line

    Returns:
        tuple[str, list[str]]: 'pt' and 'params'
    """
    space_pos = fmtpLine.find(' ')
    assert space_pos != -1
    key_values = fmtpLine[space_pos + 1:].split(';')
    result = re.search(r'a=fmtp:(\d+)', fmtpLine)
    if not result:
        return None, None
    pt = result.group(1)
    params = { key_value[0]: key_value[1] for key_value in (key_value.split('=') for key_value in key_values) if len(key_value) == 2 }
    return pt, params
    
        
def writeFmtpLine(pt: str, params: dict[str, str]) -> str | None:
    key_values = [f'{key}={value}' for key, value in params.items()]
    if len(key_values) == 0:
        return None
    return f'a=fmtp:{pt} {";".join(key_values)}'

def findFmtpLine(sdpLines: list[str], codec: str) -> int:
    payload = getCodecPayloadType(sdpLines, codec)
    return findLine(sdpLines, 'a=fmtp:' + f'{payload}') if payload is not None else -1
        

def setCodecParam(sdp: str, codec: str, param, value, mid: str | None = None) -> str:
    sdpLines = sdp.split('\r\n')
    headLines = None
    tailLines = None
    if mid:
        start, end = findMLineRangeWithMID(sdpLines, mid)
        if start is not None:
            headLines = sdpLines[0:start]
            tailLines = sdpLines[end:]
            sdpLines = sdpLines[start:end]
    if len(sdpLines) <= 1:
        sdpLines = sdp.split('\n')
    fmtpLineIndex = findFmtpLine(sdpLines, codec)
    if fmtpLineIndex == -1:
        index = findLine(sdpLines, 'a=rtpmap', codec)
        if index == -1:
            return sdp
        payload = getCodecPayloadTypeFromLine(sdpLines[index])
    else:
        pt, params = parseFmtpLine(sdpLines[fmtpLineIndex])
        assert pt is not None
        assert params is not None
        params[param] = f'{value}'
        fmtpline = writeFmtpLine(pt, params)
        assert fmtpline is not None
        sdpLines[fmtpLineIndex] = fmtpline
    if headLines:
        assert tailLines
        sdpLines = [*headLines, *sdpLines, *tailLines]
    sdp = '\r\n'.join(sdpLines)
    return sdp
# Following codecs will not be removed from SDP event they are not in the
# user-specified codec list.
audioCodecAllowList = ['CN', 'telephone-event']
videoCodecAllowList = ['red', 'ulpfec', 'flexfec']

def removeCodecFramALine(sdpLines: list[str], payload: str) -> list[str]:
    new_sdp_lines: list[str] = []
    for line in sdpLines:
        if not re.search(f'a=(rtpmap|rtcp-fb|fmtp):{re.escape(payload)}\\s', line):
            new_sdp_lines.append(line)
    return new_sdp_lines

def setCodecOrder(mLine: str, payloads: list[str]) -> str:
    """
    Returns a new m= line with the specified codec order.

    Args:
        mLine (str): m= line
        payloads (list[str]): payloads

    Returns:
        str: new m= line
    """
    elements = mLine.split(' ')
    # Just copy the first three parameters; codec order starts on fourth.
    new_line = elements[0:3]
    new_line = [*new_line, *payloads]
    return ' '.join(new_line)

def appendRtxPayloads(sdpLines: list[str], payloads: list[str]) -> list[str]:
    new_payloads = [*payloads]
    for payload in payloads:
        index = findLine(sdpLines, 'a=fmtp', f'apt={payload}')
        if index != -1:
            pt, _ = parseFmtpLine(sdpLines[index])
            assert pt is not None
            new_payloads.append(pt)
    return new_payloads

def reorderCodecs(sdp: str, type: Literal['audio', 'video'], codecs: list[str], mid: str | None = None) -> str:
    if not codecs:
        return sdp
    codecs = [*codecs, *(audioCodecAllowList if type == 'audio' else videoCodecAllowList)]
    sdpLines = sdp.split('\r\n')
    headLines = None
    tailLines = None
    if mid:
        start, end = findMLineRangeWithMID(sdpLines, mid)
        if start is not None:
            headLines = sdpLines[0:start]
            tailLines = sdpLines[end:]
            sdpLines = sdpLines[start:end]
    mLineIndex = findLine(sdpLines, 'm=', type)
    if mLineIndex == -1:
        return sdp
    originPayloads = sdpLines[mLineIndex].split(' ')
    originPayloads = originPayloads[3:]
    payloads: list[str] = []
    for codec in codecs:
        i = 0
        while i < len(sdpLines):
            index = findLineInRange(sdpLines, i, -1, 'a=rtpmap', codec)
            if index != -1:
                payload = getCodecPayloadTypeFromLine(sdpLines[index])
                if payload:
                    payloads.append(payload)
                    i = index
            i += 1
    payloads = appendRtxPayloads(sdpLines, payloads)
    sdpLines[mLineIndex] = setCodecOrder(sdpLines[mLineIndex], payloads)
    for payload in originPayloads:
        if payloads.count(payload) == 0:
            sdpLines = removeCodecFramALine(sdpLines, payload)
    if headLines:
        assert tailLines is not None
        sdpLines = [*headLines, *sdpLines, *tailLines]
    sdp = '\r\n'.join(sdpLines)
    return sdp

def setMaxBitrate(sdp: str, encodingParametersList: list[AudioEncodingParameters] | list[VideoEncodingParameters], mid: str | None):
    for p in encodingParametersList:
        if p.maxBitrate is not None and p.codec is not None:
            sdp = setCodecParam(sdp, p.codec.name, 'x-google-max-bitrate', p.maxBitrate, mid)
    return sdp