# T4 benchmark — reproducible evaluation environment.
#
# Build:  docker build -t t4-benchmark .
# Run:    docker run --rm t4-benchmark            # runs `make reproduce-small`
# Shell:  docker run --rm -it t4-benchmark bash   # inspect / run other targets
#
# The image pins Go 1.24 (bookworm) and installs the Python plotting stack from
# Debian packages so the whole pipeline (Go harness -> pilot JSON -> figures) is
# self-contained and needs no network access at run time.

FROM golang:1.24-bookworm

# Python 3 + matplotlib + numpy for analysis/make_figures.py. Using the Debian
# packages keeps the image hermetic and avoids pip resolving against the network.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-matplotlib \
        python3-numpy \
        make \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /t4

# Copy the module. (The build uses only the standard library; there is nothing
# to `go mod download`.)
COPY . .

# Pre-build so the image is ready to reproduce immediately.
RUN go build ./...

# Default: reproduce the pilot end to end (tests + pilot + figures).
CMD ["make", "reproduce-small"]
