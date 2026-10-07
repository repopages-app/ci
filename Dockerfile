# ghcr.io/repopages-app/ci: the RepoPages CI client with diagram renderers, for CI systems that
# have neither Chrome nor Java (GitLab CI, Jenkins, Bitbucket Pipelines, ...).
#
# Build:  docker build -t repopages-ci:dev .
# Use:    docker run --rm -v "$PWD:/repo" -w /repo -e REPOPAGES_URL -e REPOPAGES_SECRET ghcr.io/repopages-app/ci:1 repopages push --repo owner/name
#
# Contents: Python 3.12 (runs the client), Node 24 + @mermaid-js/mermaid-cli 11 + Debian Chromium
# (Mermaid), a headless JRE + plantuml.jar (PlantUML; its built-in Smetana layout is used when
# Graphviz is missing, so class/component diagrams work without it), git. For CJK labels add
# fonts-noto-cjk (~90 MB) in a derived image.
FROM node:24-trixie-slim AS node

FROM python:3.12-slim-trixie

ARG MERMAID_CLI_VERSION=11
ARG PLANTUML_VERSION=1.2026.8

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm

RUN set -eux; \
    ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm; \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        git ca-certificates chromium default-jre-headless fonts-dejavu-core fonts-liberation; \
    rm -rf /var/lib/apt/lists/*; \
    # The repo is mounted from the host and usually owned by another uid.
    git config --system --add safe.directory '*'

# Mermaid CLI uses the Debian Chromium instead of downloading its own browser.
ENV PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    PLANTUML_JAR=/opt/plantuml/plantuml.jar

RUN npm install -g --omit=dev "@mermaid-js/mermaid-cli@${MERMAID_CLI_VERSION}" && npm cache clean --force

# PlantUML, MIT-licensed build, pinned by checksum.
ADD --chmod=644 --checksum=sha256:3629c9cd017c7f73e6450396eea0040216c7e1eef8473ce33cc1aad469dab2f9 \
    https://github.com/plantuml/plantuml/releases/download/v${PLANTUML_VERSION}/plantuml-mit-${PLANTUML_VERSION}.jar \
    /opt/plantuml/plantuml.jar

COPY repopages_push.py mermaid-config.json /opt/repopages/
RUN set -eux; \
    printf '#!/bin/sh\nexec python3 /opt/repopages/repopages_push.py "$@"\n' > /usr/local/bin/repopages; \
    chmod 755 /usr/local/bin/repopages; \
    repopages selftest

CMD ["repopages", "--help"]
