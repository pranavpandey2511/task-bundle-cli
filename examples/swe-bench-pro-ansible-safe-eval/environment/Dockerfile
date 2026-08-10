# The official SWE-bench Pro instance image is linux/amd64-only. This
# digest-pinned multi-architecture Python 3.11 image reconstructs the selected
# unit-test environment natively on the ARM64 validation host.
FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential git libffi-dev libssl-dev openssh-client rsync sshpass \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY source/ /workspace/

RUN python -m pip install --no-cache-dir \
        "pip==25.3" \
        "setuptools==80.9.0" \
        "wheel==0.45.1" \
    && python -m pip install --no-cache-dir \
        "Jinja2==3.1.6" \
        "PyYAML==6.0.3" \
        "cryptography==43.0.3" \
        "packaging==24.2" \
        "resolvelib==1.0.1" \
        "mock==5.2.0" \
        "pytest==8.3.4" \
        "pytest-mock==3.14.0" \
        "pytest-xdist==3.6.1" \
    && python -m pip install --no-cache-dir --no-deps --editable .

ENV PYTHONPATH=/workspace/lib:/workspace/test/lib:/workspace/test
ENV PATH=/workspace/bin:$PATH
ENV ANSIBLE_VERBOSITY=1
ENV PYTEST_ADDOPTS="--tb=short -v --continue-on-collection-errors"
