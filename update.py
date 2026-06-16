import json
from pathlib import Path

import requests
import typer
from bs4 import BeautifulSoup
from lxml import etree, objectify  # type: ignore[import-untyped]
from typer import colors, secho

from fileidentification.definitions.settings import FMTJSN, DroidSigURL


def write_fmt2ext(link: str) -> None:
    # tmp xml_filname
    xml_filename = Path(f"droid_{link[-8:]}")

    # get the droid xml content and save it to file (as it is large and to avoid error on etree.parse)
    res = requests.get(link, timeout=10)
    if res.status_code != 200:
        secho(f"could not fetch {link}", fg=colors.RED)
        raise typer.Exit(1)
    xml_filename.write_text(res.content.decode("utf-8"))

    # open XML file and strip namespaces, delete the xml
    tree = etree.parse(xml_filename)
    xml_filename.unlink()
    root = tree.getroot()
    for elem in root.getiterator():
        if not hasattr(elem.tag, "find"):
            continue
        i = elem.tag.find("}")
        if i >= 0:
            elem.tag = elem.tag[i + 1 :]
    objectify.deannotate(root, cleanup_namespaces=True)

    # parse XML and write json
    puids: dict[str, dict[str, str | list[str]]] = {}

    for target in root.findall(".//FileFormat"):
        format_info: dict[str, str | list[str]] = {}
        file_extensions: list[str] = []

        puid = target.attrib["PUID"]

        if target.attrib["Name"]:
            format_info["name"] = target.attrib["Name"]

        file_extensions.extend([extens.text for extens in target.findall(".//Extension")])

        format_info["file_extensions"] = file_extensions

        puids[puid] = format_info

    FMTJSN.write_text(json.dumps(puids, indent=4, ensure_ascii=False))
    secho(
        f"extensions and names updated to {link[-8:-4]} in {FMTJSN}",
        fg=colors.GREEN,
    )


def update_signatures() -> None:
    # get the latest signaturefile link
    secho(f"... updating {FMTJSN}")
    url = DroidSigURL.NALIST
    res = requests.get(url, timeout=10)
    if res.status_code != 200:
        secho(f"could not fetch {url}", fg=colors.RED)
        raise typer.Exit(1)

    soup = BeautifulSoup(res.content, "html.parser")
    versions = [
        el.get("href")
        for el in soup.find_all("a")
        if el.get("href") and el.get("href").startswith(DroidSigURL.CDN)  # type: ignore[union-attr]
    ]

    link = sorted(versions)[-1]  # type: ignore[type-var]
    if not link:
        secho(f"could not parse links out of {url}", fg=colors.RED)
        raise typer.Exit(1)
    # update fm
    write_fmt2ext(link=link)  # type: ignore[arg-type]


if __name__ == "__main__":
    typer.run(update_signatures)
