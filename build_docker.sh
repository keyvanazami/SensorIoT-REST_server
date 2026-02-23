set BUILDKIT_PROGRESS=plain
docker build --progress plain -t sensoriot_server  . 2>&1
