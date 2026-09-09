import tarfile
from typing import BinaryIO

from dryheave.errors import IntegrityError, LimitError

MAX_EXTENSION_BYTES = 65536
MAX_EXTENSION_CHAIN = 16
EXTENSIONS = {
    tarfile.XHDTYPE,
    tarfile.XGLTYPE,
    tarfile.SOLARIS_XHDTYPE,
    tarfile.GNUTYPE_LONGNAME,
    tarfile.GNUTYPE_LONGLINK,
}
PAX_EXTENSIONS = {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE}


def _validate_pax(content: bytes) -> None:
    offset = 0
    while offset < len(content):
        space = content.find(b" ", offset)
        digits = content[offset:space] if space >= 0 else b""
        if not digits.isdigit() or len(digits) > len(str(MAX_EXTENSION_BYTES)):
            raise IntegrityError("Bundle PAX metadata has an invalid record length.")
        end = offset + int(digits)
        if end > len(content) or end <= space + 1 or content[end - 1 : end] != b"\n":
            raise IntegrityError("Bundle PAX metadata has a truncated record.")
        key, separator, _value = content[space + 1 : end - 1].partition(b"=")
        if not separator:
            raise IntegrityError("Bundle PAX metadata has an invalid record.")
        if (
            key == b"size"
            or key.startswith(b"GNU.sparse.")
            or key in {b"SCHILY.realsize", b"SCHILY.filetype"}
        ):
            raise IntegrityError("Bundle PAX structural or sparse overrides are unsupported.")
        offset = end


def preflight_tar(stream: BinaryIO, *, max_bytes: int, max_files: int) -> None:
    stream.seek(0, 2)
    length = stream.tell()
    stream.seek(0)
    chain = entries = total = 0
    while True:
        header = stream.read(tarfile.BLOCKSIZE)
        if header in (b"", bytes(tarfile.BLOCKSIZE)):
            if chain:
                raise IntegrityError("Bundle ends inside an extension header chain.")
            break
        try:
            member = tarfile.TarInfo.frombuf(header, "utf-8", "surrogateescape")
        except tarfile.HeaderError as error:
            raise IntegrityError("Bundle contains an invalid physical tar header.") from error
        entries += 1
        if entries > 2 * max_files + MAX_EXTENSION_CHAIN:
            raise LimitError("Bundle exceeds its physical header bound.")
        extension = member.type in EXTENSIONS
        size = member.size
        if extension and (size > MAX_EXTENSION_BYTES or chain >= MAX_EXTENSION_CHAIN):
            raise LimitError("Bundle exceeds its tar extension metadata bounds.")
        if size < 0 or stream.tell() + size > length:
            raise IntegrityError("Bundle entry is truncated or has an invalid size.")
        total += size
        if total > max_bytes:
            raise LimitError("Bundle exceeds its physical content bound.")
        if extension:
            chain += 1
            content = stream.read(size)
            if member.type in PAX_EXTENSIONS:
                _validate_pax(content)
        else:
            if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.CONTTYPE}:
                raise IntegrityError("Bundle contains a non-regular physical tar entry.")
            stream.seek(size, 1)
            chain = 0
        stream.seek((-size) % tarfile.BLOCKSIZE, 1)
    stream.seek(0)
