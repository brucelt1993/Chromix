"""Chromix 多开管理器：profile 管理、独立指纹、CDP 接入与登录隔离。

启动：``python -m chromix manager``，浏览器打开 http://127.0.0.1:8820/。
"""
from .server import Manager, serve
from .store import ProfileStore

__all__ = ["Manager", "ProfileStore", "serve"]
