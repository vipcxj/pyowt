import asyncio
from dataclasses import dataclass


@dataclass
class QuicBidirectionalStream:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    lock: asyncio.Lock
    
    