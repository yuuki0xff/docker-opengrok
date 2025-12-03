# OpenGrok for large codebases

## Usage

```shell
cp -a example/.env example/docker-compose.override.yml ./
# Edit .env and docker-compose.override.yml

docker compose up -d

# Add/update/delete OpenGrok projects according to example/example.json
docker compose exec opengrok bash
opengrok-manager </example/example.json
```
