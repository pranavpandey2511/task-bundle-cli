# The official instance image is amd64-only and cannot execute on the ARM64
# validation host. This digest-pinned Python 3.9 image reconstructs the selected
# unit-test dependencies natively while preserving the benchmark inputs.
FROM python:3.9-slim@sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b

RUN apt-get update \
    && apt-get install --yes --no-install-recommends gcc git libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY source/ /workspace/

RUN python -m pip install --no-cache-dir \
        "pip==25.3" \
        "setuptools==75.3.2" \
        "wheel==0.45.1" \
    && python -m pip install --no-cache-dir \
        "Jinja2==2.11.3" \
        "MarkupSafe==1.1.1" \
        "PyYAML==6.0.3" \
        "cryptography==43.0.3" \
        "packaging==24.2" \
        "passlib==1.7.4" \
        "pytz==2025.2" \
        "pexpect==4.9.0" \
        "mock==4.0.3" \
        "pytest==6.2.5" \
        "pytest-xdist==2.4.0" \
    && python -m pip install --no-cache-dir --no-deps --editable .

ENV PYTHONPATH=/workspace/lib:/workspace/test/lib:/workspace/test
ENV PATH=/workspace/bin:$PATH
ENV ANSIBLE_VERBOSITY=1
ENV PYTEST_ADDOPTS="--tb=short -v --continue-on-collection-errors"
