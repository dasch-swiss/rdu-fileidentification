# Fileidentification

A python CLI to identify file formats and bulk convert files.
It is designed for digital preservation workflows and is basically a python wrapper around several programs.
It uses [pygfried](https://pypi.org/project/pygfried/)
(a CPython extension for [siegfried](https://www.itforarchivists.com/siegfried)),
ffmpeg, imagemagick (optionally inkscape) and LibreOffice.
If you are not using fileidentification a lot and don't want to install these programs,
you can run the script in a docker container.
There is a dockerfile ready, the current docker image is still heavy though (1.3 G).

Most probable use case might be when you need to test and possibly convert a huge number of files
and you don't know in advance what file types you are dealing with.
It features:

- file format identification and extraction of technical metadata with pygfried, ffprobe and imagemagick
- file probing with ffmpeg and imagemagick
- file conversion with ffmpeg, imagemagick and LibreOffice using a JSON file as a protocol
- detailed logging

## Installation

### Docker-based

Build the image, make the bash script executable,
and link it to a bin directory that appears in PATH (e.g. $HOME/.local/bin):

```bash
docker build -t fileidentification .
chmod +x ./fidr.sh
ln -s `pwd`/fidr.sh $HOME/.local/bin/fidr
```

#### Quickstart for Docker-based Installation

1. **Generate policies for your files:**

    `fidr path/to/directory`

    This creates a folder `__fileidentification` inside the target directory with a `_policies.json` file. 
    Optionally review and edit it to customize conversion rules.

2. **Test the files on errors and apply the policies:**

    `fidr path/to/directory -iar`

See **Options** and **Examples** below for more available flags. or just use `fidr --help`

### Manual Installation on Your System

Install ffmpeg (used to check/convert video and audio files),
imagemagick (used to check/convert images),
LibreOffice (used to convert Office documents and PDFs),
and ghostscript (used by imagemagick when dealing with PDFs):

#### MacOS (using Homebrew)

```bash
brew install ffmpeg
brew install imagemagick
brew install ghostscript
brew install --cask libreoffice
```

#### Linux

Depending on your distribution:

- [ffmpeg](https://ffmpeg.org/download.html#build-linux)
- [imagemagick](https://imagemagick.org/script/download.php#linux)
- [LibreOffice](https://www.libreoffice.org/download/download-libreoffice)

On Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install ffmpeg imagemagick ghostscript libreoffice
```

#### Python Dependencies

If you don't have [uv](https://docs.astral.sh/uv/) installed, install it with

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then, you can use `uv run` to run the fileidentification script.
This creates a venv and installs all necessary python dependencies:

```bash
uv run identify.py --help
```

## Main Tasks Explained

`uv run identify.py` is equivalent to `fidr` -> the entry script depending on your installation.

### Detect File Formats - Generate Conversion Policies

`uv run identify.py path/to/directory`

This generates a folder `__fileidentification` inside the target directory with two JSON files:

**_log.json** : The technical metadata of all the files in the folder

**_policies.json** : A file conversion protocol for each file format
that was encountered in the folder according to the default policies. Edit it to customize conversion rules.

#### Overview table

In the console output there is a table with an overview of the encountered file formats.
The rows are colored according to this color code:

- White: Files of that format are processed according to the given policy
- Yellow: Files of that format are skipped (either blank policy or policy is missing)
- Red: Files of that format are being removed (when run with flag `-s`, `--strict`)

Possible values of the "Policy" column:

- `ffmpeg|magick|soffice`: Files of this format are going to be converted with the indicated program
- blank: Generated a blank policy (template)
- missing: No policy for this file type

### Assert File Integrity

(`-i` | `--assert-file-integrity`)

`uv run identify.py path/to/directory -i`

Probe the files on errors and move corrupted files to the folder in `__fileidentification/_REMOVED`.
Rename files with extension mismatch.

Optionally add the flag `-v` (`--verbose`) for more detailed inspection (see **Options** below).

NOTE: Currently only audio/video and image files are inspected.

### Convert The Files According to the Policies

(`-a` | `--apply`)

`uv run identify.py path/to/directory -a`

Apply the policies defined in `__fileidentification/_policies.json` (or in the policies passed with `-p`) and convert
files into their target file format.
The converted files are temporarily stored in `__fileidentification`

### Clean Up Temporary Files

(`-r` | `--remove-tmp`)

`uv run identify.py path/to/directory -r`

Delete all temporary files and folders and move the converted files next to their original.

### Log

The **_log.json** takes track of all modifications in the target folder.  
Since with each execution of the script it checks whether such a log exists and read/appends to that file.  
Iterations of file conversions such as A -> B, B -> C, ... are logged in the same file.

If you wish a simpler csv output, you can add the flag `--csv` anytime when you run the script,
which maps the `_log.json` to a csv.

## Advanced Usage

You can also create your own policies, and with that, customise the file conversion output.
Simply edit the generated default file `__fileidentification/_policies.json` before applying or pass a customised
policies files with the parameter `-p`.
If you want to start from scratch, run `uv run indentify.py path/to/directory -b` to create a
blank policies template with all the file formats encountered in the folder.

### Policy Specification

A policy for a file type consists of the following fields and uses its PRONOM Unique Identifier (PUID) as a key

| Field                | Type           |                                     |
|----------------------|----------------|-------------------------------------|
| **format_name**      | **str**        | optional                            |
| **bin**              | **str**        | required                            |
| **accepted**         | **bool**       | required                            |
| **target_container** | **str**        | required if field accepted is false |
| **processing_args**  | **str**        | required if field accepted is false |
| **expected**         | **list[str]**  | required if field accepted is false |
| **remove_original**  | **bool**       | optional (default is `false`)       |

- `format_name`: The name of the file format.
- `bin`: Program to convert or test the file. Literal[`""`, `"magick"`, `"ffmpeg"`, `"soffice"`].
(Testing currently only is supported on image/audio/video, i.e. ffmpeg and magick.)
- `accepted`: `false` if the file needs to be converted, `true` if it doesn't.
- `processing_args`: The arguments used with bin. Can also be an empty string if there is no need for such arguments.
- `expected`: the expected file format for the converted file as PUID
- `remove_original`: whether to keep the parent of the converted file in the directory, default is `false`

### Policy Examples

A policy for Audio/Video Interleaved Format (avi) that need to be transcoded to MPEG-4 Media File
(Codec: AVC/H.264, Audio: AAC) looks like this:

```json
{
    "fmt/5": {
        "format_name": "Audio/Video Interleaved Format",
        "bin": "ffmpeg",
        "accepted": false,
        "target_container": "mp4",
        "processing_args": "-c:v libx264 -crf 18 -pix_fmt yuv420p -c:a aac",
        "expected": [
            "fmt/199"
        ],
        "remove_original": false
    }
}
```

A policy for Portable Network Graphics that is accepted as it is:

```json
{
    "fmt/13": {
        "format_name": "Portable Network Graphics",
        "bin": "magick",
        "accepted": true
    }
}
```

**Policy Testing:**

You can test the outcome of the conversion policies with

`uv run identify.py path/to/directory -t`

The script takes the smallest file for each conversion policy and converts it.

If you just want to test a specific policy, append `f` and the puid:

`uv run identify.py path/to/directory -tf fmt/XXX`

## Options

`-i` | `--assert-file-integrity`  
Remove corrupt files, and try to fix minor errors

`-v` | `--verbose`  
Catch more warnings on video and image files during the tests.
This can take a significantly longer time based on what files you have.

`-a` | `--apply`  
Apply the policies

`-r` | `--remove-tmp`  
Remove all temporary items and add the converted files next to their original.

`-x` | `--remove-original`  
This overwrites the `remove_original` value in the policies and sets it to true when removing the tmp files.
The original files are moved to the `__fileidentification/_REMOVED` folder.
When used in generating policies, it sets `remove_original` in the policies to true (default false).

`--tmp-dir`  
Use a custom tmp directory instead of the default `__fileidentification`

`-p` | `--policies-path`  
Load a custom policies JSON file instead of generating one out of the default policies.

`-e` | `--extend-policies`  
Use with `-p`:

Append filetypes found in the directory to the custom policies if they are missing in it and generate a
new policies json.

`-s` | `--strict`  
Move the files whose format is not listed in the policies file to the folder _REMOVED
(instead of emitting a warning).
When used in generating policies, do not add blank policies for formats that are not mentioned in DEFAULTPOLICIES.

`-b` | `--blank`  
Create a blank policies based on the file types encountered in the given directory.

`-q` | `--quiet`  
Just print errors and warnings

`--inspect`  
Just inspect the target folder without any modification

`--csv`  
Get output as CSV, in addition to the log.json

`--convert`  
Re-convert the files that failed during file conversion


### Examples

Use case: you have defined a set of rules in an external policies file and want to remove files of any format that
is not listed in the external policies.  
To get an overview: `fidr path/to/directory -s -p path/to/external_policies.json`  
To apply it directly:

`fidr path/to/directory -asr -p path/to/external_policies.json`

- load an external policies JSON
- apply the policies (in strict mode, i.e. remove the files whose file type is not listed in the policies)
- remove temporary files  

Use case: Your files are on an external storage drive and you might have limited diskspace left
and want to only keep the converted files.

`fidr path/to/directory --tmp-dir path/to/tmp_dir -ivarx`

- use a custom tmp_dir to write files to (instead of the default `path/to/directory/__fileidentification`)
- probe the files in verbose mode and apply the policies
- remove temporary files and the original of the converted files

## Updating the PUIDs

Update the file format names and extensions of the PUIDs according to <https://www.nationalarchives.gov.uk/>.

```bash
uv sync --extra update_fmt && uv run update.py
```

creates an updated version of `fileidentification/definitions/fmt2ext.json`.
If you use the Docker-based version, don't forget to rebuild the Docker image after updating the PUIDs.

## Useful Links

You'll find a good resource to query for fileformats on
[nationalarchives.gov.uk](https://www.nationalarchives.gov.uk/PRONOM/Format/proFormatSearch.aspx?status=new)

The Homepage of siegfried
[itforarchivists.com/siegfried/](https://www.itforarchivists.com/siegfried/)

List of File Signatures on
[wikipedia](https://en.wikipedia.org/wiki/List_of_file_signatures)

Preservation recommendations
[kost](https://kost-ceco.ch/cms/de.html)
[bundesarchiv](https://www.bar.admin.ch/dam/bar/de/dokumente/konzepte_und_weisungen/archivtaugliche_dateiformate.1.pdf.download.pdf/archivtaugliche_dateiformate.pdf)
