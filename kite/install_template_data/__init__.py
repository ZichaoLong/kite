"""Package-data container for install-time templates.

``system.yaml.example`` ships inside the installed package (declared as
package data in pyproject.toml) so installed deployments can scaffold an
instance without reaching back into the source tree
(kite/install_templates.py is the only reader).
"""
