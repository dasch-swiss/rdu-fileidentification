# Justfile for fileidentification installation
# Provides convenient shortcuts

# Default recipe - show available commands
default:
    @just --list

# get latest version from git and reset
gitreset: 
    git fetch
    git reset --hard HEAD
    git merge '@{u}'
    @echo "reset to latest git version"

# create docker image and link entry script
dockerise:
    docker build -t fileidentification .
    @if [ ! -d $HOME/.local/bin ]; then mkdir -p $HOME/.local/bin && echo "export PATH=\"${HOME}/.local/bin:\$PATH\"" | tee -a $HOME/.{bash,zsh}rc ; fi
    chmod +x ./fidr.sh
    @if [ -L $HOME/.local/bin/fidr ]; then rm $HOME/.local/bin/fidr ; fi
    ln -s `pwd`/fidr.sh $HOME/.local/bin/fidr
    @echo "created docker image, added fidr to path"

# set DaSCH policies as default
setdasch:
    mv -f policies/dasch_policies.json fileidentification/definitions/default_policies.json
    
# DaSCH docker installation
dasch: gitreset setdasch dockerise

# generic docker installation
docker: gitreset dockerise

# manual installation macOS
install:
    uv sync --no-group dev
    brew install ffmpeg imagemagick ghostscript
    brew install --cask libreoffice

# Update dependencies
update:
    uv lock --upgrade

# Run type checking with mypy (if configured)
typecheck:
    uv run mypy .

# Format code with ruff
format:
    uv run ruff format .

# Lint code with ruff
lint:
    uv run ruff check .

# Lint and fix issues automatically
lint-fix:
    uv run ruff check --fix .

# Run all checks: lint and typecheck
check: lint typecheck

# Run the fast unit tests (no docker needed)
test:
    uv run pytest -m "not docker"

# Build the image and run the docker end-to-end tests
test-docker:
    docker build -t fileidentification .
    uv run pytest -m docker

# Build the image and run the whole test suite
test-all:
    docker build -t fileidentification .
    uv run pytest
