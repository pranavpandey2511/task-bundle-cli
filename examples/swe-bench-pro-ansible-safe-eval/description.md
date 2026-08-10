# Deprecate safe evaluation in Ansible dictionary validation

Ansible still exposes `safe_eval` through both
`ansible.module_utils.common.validation` and `AnsibleModule.safe_eval`.
The dictionary validator also falls back to evaluation when JSON parsing fails.
Replace that behavior with a deterministic, non-executing parsing path and make
the legacy entry points emit a deprecation warning targeted at Ansible 2.21.

`check_type_dict` must continue to accept dictionaries, JSON objects, and
simple `key=value` pairs separated by commas or spaces. It must return only a
dictionary and raise a clear `TypeError` for malformed, incomplete, or
non-dictionary inputs. The parsing path must not execute arbitrary user input.

Add the matching changelog deprecation entry. The relevant implementation is in:

- `lib/ansible/module_utils/common/validation.py`
- `lib/ansible/module_utils/basic.py`
- `changelogs/fragments/deprecate-safe-evals.yml`
