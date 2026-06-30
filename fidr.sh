#!/bin/bash
# parse the args and options and store potential paths for volumes to mount in docker
# set relative path to absolute
add_volumes=()
params=()
while [ $# -gt 0 ]; do
    # options
    if [[ $1 == "-p" ]] || [[ $1 == "-ep" ]] || [[ $1 == "--policies-path" ]]; then
        policies_path=$(realpath "$2")
        add_volumes+=("-v" "$policies_path:$policies_path")
        params+=("$1" "$policies_path")
        shift 2
    fi
    if [[ $1 == "--tmp-dir" ]]; then
        mkdir -p "$2"
        tmp_dir=$(realpath "$2")
        add_volumes+=("-v" "$tmp_dir:$tmp_dir")
        params+=("$1" "$tmp_dir")
        shift 2
    fi
    if [[ $1 == "-"* ]] || [[ $1 == "--"* ]]; then
      params+=("$1")
      shift
    fi
    # input folder (argument)
    if [ ! -z "$1" -a "$1" != " " ] && [[ $1 != "-"* ]]; then
      # assert input folder
      if [[ !  $(realpath "$1") ]]; then
        exit 1
      fi
      input_dir=$(realpath "$1")
      mnt_dir="$input_dir"
      # if its a file
      if [[ -f $1 ]]; then
        mnt_dir="${mnt_dir%/*}"
      fi
      add_volumes+=("-v" "$mnt_dir:$mnt_dir")
      shift
    fi
done

# allocate a TTY by default (nicer interactive output); set FIDR_NO_TTY=1 to
# disable it for headless/non-interactive use (e.g. CI, automated tests)
tty_opt=()
if [ -z "${FIDR_NO_TTY:-}" ]; then
    tty_opt+=("-t")
fi

# run the command
docker run --rm "${add_volumes[@]}" "${tty_opt[@]}" fileidentification "${params[@]}" "$input_dir"
