# OpenGrok for large codebases

## Usage

```shell
cp -a example/.env example/compose.override.yaml ./
# Edit .env and compose.override.yaml

docker compose up -d

# Add/update/delete OpenGrok projects according to example/example.json
docker compose exec opengrok bash
opengrok-manager </example/example.json
```
