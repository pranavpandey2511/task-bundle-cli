# Handle undefined variables in Ansible's YAML dumper

Ansible's `to_yaml` and `to_nice_yaml` filters currently expose a low-level
`yaml.representer.RepresenterError` when a template contains an undefined
variable. For example, piping a missing variable through `to_nice_yaml` while
rendering a template produces “cannot represent an object, AnsibleUndefined”.

Change the YAML dumping path so an `AnsibleUndefined` value is treated as an
undefined-variable condition from the templating layer. The YAML dump must not
silently serialize it or coerce it to `None`.

Both `to_yaml` and `to_nice_yaml` must surface dumping failures as a clear
`AnsibleFilterError`, identify the filter in the error message, and preserve the
underlying exception for debugging. Existing supported YAML values must keep
working.

The relevant code is in:

- `lib/ansible/parsing/yaml/dumper.py`
- `lib/ansible/plugins/filter/core.py`
