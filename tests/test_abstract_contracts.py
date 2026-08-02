from typing import Any, cast

import pytest

from GensokyoAI.adapters import RuntimeAdapter
from GensokyoAI.background.workers.base import BaseWorker


def test_runtime_adapter_cannot_be_instantiated() -> None:
    adapter_class = cast(Any, RuntimeAdapter)
    with pytest.raises(TypeError):
        adapter_class()


def test_base_worker_cannot_be_instantiated() -> None:
    worker_class = cast(Any, BaseWorker)
    with pytest.raises(TypeError):
        worker_class()
