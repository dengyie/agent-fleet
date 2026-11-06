"""Composition steps behind :func:`hub.bootstrap.create_app`.

Each module builds one cohesive slice of the hub (platform services, session
stores, core observe/task services, the supervisor control plane) or wires the
built slices into the Flask app (config, extensions, routes).  Every module
keeps its project imports inside functions, so importing this package adds no
startup cost and disabled feature gates import nothing.
"""
