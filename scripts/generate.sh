#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
version=7.16.0
checksum=6999b18cece5b58f5d5b246fef5a43bdf61239491c4f0ed6513214e0f6e8464b
jar=".cache/openapi-generator-cli-$version.jar"
mkdir -p .cache
if [ ! -f "$jar" ]; then
    curl -fsSL --retry 3 "https://repo.maven.apache.org/maven2/org/openapitools/openapi-generator-cli/$version/openapi-generator-cli-$version.jar" -o "$jar.tmp"
    mv "$jar.tmp" "$jar"
fi
printf '%s  %s\n' "$checksum" "$jar" | sha256sum -c -
java -jar "$jar" validate -i openapi/marketplace.yaml
rm -rf generated
java -jar "$jar" generate -g python-fastapi -i openapi/marketplace.yaml \
    -c codegen/config.yaml -t codegen/templates -o generated \
    --global-property=apiTests=false,modelTests=false,apiDocs=false,modelDocs=false
