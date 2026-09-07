set quiet
set default-list := true

docker_compose_file := "-f docker-compose.yml"
service_name := "mindbackup"
user_specification := "--user $(id -u)"

docker_compose_executable := if `which docker-compose || echo ""` != "" { "docker-compose" } else {"docker compose" }

help: 
	echo "Run just --list"

build:
    {{ docker_compose_executable }} {{ docker_compose_file }} build {{ service_name }}

manual-ingest:
    echo "Input audio path (without file name)" && read path && echo "Input audio file name" && read name && {{ docker_compose_executable }} run --rm -v $path:/opt/audios {{ service_name }} ingest /opt/audios/$name

doctor:
	{{ docker_compose_executable }} run --rm {{ service_name }} doctor

extract-dry-run:
	{{ docker_compose_executable }} run --rm {{ service_name }} extract --dry-run --limit 1

extract:
	{{ docker_compose_executable }} run --rm {{ service_name }} extract

ask:
	echo "What do you want to know?" && read query && {{ docker_compose_executable }} run --rm {{ service_name }} ask $query

start:
	{{ docker_compose_executable }} {{ docker_compose_file }} up {{ service_name }}

stop:
	{{ docker_compose_executable }} {{ docker_compose_file }} down {{ service_name }}

bash:
	{{ docker_compose_executable }} {{ docker_compose_file }} exec {{ user_specification }} {{ service_name }} bash	

bash-root:
	{{ docker_compose_executable }} {{ docker_compose_file }} exec {{ service_name }} bash