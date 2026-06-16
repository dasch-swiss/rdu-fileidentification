import json
from enum import StrEnum
from pathlib import Path
from typing import Any

# default policies
DEFAULTPOLICIES: Path = Path(__file__).parent / "default_policies.json"

# max number of files processed concurrently in inspect / assert_integrity / apply_policies / convert
MAX_WORKERS: int = 4

# paths tmp dir, logs
TMP_DIR = "__fileidentification"  # added to root folder
LOGJSON = "_log.json"
POLJSON = "_policies.json"
RMV_DIR = "_REMOVED"


# application settings
class DroidSigURL(StrEnum):
    """urls to fetch droid signature xml from national archives"""

    NALIST = "https://www.nationalarchives.gov.uk/aboutapps/pronom/droid-signature-files.htm"
    CDN = "https://cdn.nationalarchives.gov.uk/documents/DROID_SignatureFile_"


# dict that resolves the puid to possible ext and file format name
FMTJSN: Path = Path(__file__).parent / "fmt2ext.json"
FMT2EXT: dict[str, Any] = json.loads(FMTJSN.read_text())


class Bin(StrEnum):
    MAGICK = "magick"
    FFMPEG = "ffmpeg"
    SOFFICE = "soffice"
    # INCSCAPE = "inkscape"
    EMPTY = ""


class LOPath(StrEnum):
    """path where LibreOffice exec is according to os"""

    Darwin = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    Linux = "libreoffice"


# it needs libreoffice v7.4 + for this to work, set to pdf/A version 2
PDFSETTINGS = ':writer_pdf_Export:{"SelectPdfVersion":{"type":"long","value":"2"}}'


CSVFIELDS = [
    "status",
    "filename",
    "filesize",
    "md5",
    "modified",
    "errors",
    "processed_as",
    "warnings",
    "media_info",
    "processing_logs",
    "derived_from",
]


# msg


class PVErr(StrEnum):
    """policy validation errors"""

    SEMICOLON = "the char ';' is not an allowed in processing_args"
    MISS_CON = "missing 'target_container' in policy"
    MISS_EXP = "missing 'expected' in policy"
    MISS_BIN = "missing bin in policy"


class PLMsg(StrEnum):
    """policy log messages"""

    FALLBACK = "fmt not detected, fallback on extension"
    NOTINPOLICIES = "file format is not in policies and strict is set to true"
    SKIPPED = "file format is not in policies, skipped"


class FDMsg(StrEnum):
    """file diagnostic message"""

    EMPTYSOURCE = "empty source"
    ERROR = "file is corrupt"
    WARNING = "file has warnings"
    EXTMISMATCH = "extension mismatch"


class FPMsg(StrEnum):
    """file processing message"""

    PUIDFAIL = "failed to get fmt type"
    CONVFAILED = "conversion failed"
    NOTEXPECTEDFMT = "converted file does not match the expected fmt."


class REencMsg(StrEnum):
    """text in log for smaller errors that can be solved with re encoding the file"""

    ffmpeg1 = "A non-intra slice in an IDR NAL unit"


class ErrMsgIM(StrEnum):
    """text in warnings that indicate that the file is not or only partially readable"""

    magic1 = "identify: Cannot read"
    magic2 = "identify: Sanity check on directory count failed"
    magic3 = "identify: Failed to read directory"
    magic4 = "identify: insufficient image data in file "
    magic5 = "premature end of data segment"
