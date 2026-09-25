set quiet
set default-list := true

docker_compose_file := "-f docker-compose.yml"
service_name := "mindbackup"
user_specification := "--user $(id -u)"

docker_compose_executable := if `which docker-compose || echo ""` != "" { "docker-compose" } else {"docker compose" }

help: 
	echo "Run just --list"

# Interactive first-time setup (`--list` shows the steps, `--step NAME` re-runs one)
setup *args:
	bash scripts/setup.sh {{ args }}

build:
    {{ docker_compose_executable }} {{ docker_compose_file }} build {{ service_name }}

manual-ingest:
    echo "Input audio path (without file name)" && read path && echo "Input audio file name" && read name && {{ docker_compose_executable }} run --rm -v $path:/opt/audios {{ service_name }} ingest /opt/audios/$name

doctor:
	{{ docker_compose_executable }} run --rm {{ service_name }} doctor

extract-dry-run:
	{{ docker_compose_executable }} run --rm {{ service_name }} extract --dry-run --limit 1

extract-interactive:
	{{ docker_compose_executable }} run --rm {{ service_name }} extract 2026-09-18_3.md --interactive

extract:
	{{ docker_compose_executable }} run --rm {{ service_name }} extract

ask:
	echo "What do you want to know?" && read query && {{ docker_compose_executable }} run --rm {{ service_name }} ask $query

# Admin: delete a memo plus its atoms and topic bullets. Asks before deleting.
# e.g. `just delete-memo 2026-09-18_3` or `just delete-memo 2026-09-18_3 --dry-run`
delete-memo memo *flags:
	{{ docker_compose_executable }} run --rm {{ service_name }} delete {{ memo }} {{ flags }}

start:
	{{ docker_compose_executable }} {{ docker_compose_file }} up {{ service_name }}

stop:
	{{ docker_compose_executable }} {{ docker_compose_file }} down {{ service_name }}

bash:
	{{ docker_compose_executable }} {{ docker_compose_file }} exec {{ user_specification }} {{ service_name }} bash	

bash-root:
	{{ docker_compose_executable }} {{ docker_compose_file }} exec --user root {{ service_name }} bash

test:
	uv run --extra dev pytest -q .

lint:
	uv run --extra dev ruff check src tests

# Not checked by `just lint`: the repo is not ruff-format clean yet, and
# running this reformats ~18 files in one go. Opt in deliberately.
format:
	uv run --extra dev ruff format src tests
	uv run --extra dev ruff check --fix src tests
