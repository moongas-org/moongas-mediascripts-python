from typing import Any, Callable

class YAMLWizard:
    def __init_subclass__(
        cls,
        *,
        key_transform: Any = ...,
        **kwargs: Any,
    ) -> None: ...

    def to_yaml(
        self,
        *,
        encoder: Callable[..., Any] | None = ...,
        **encoder_kwargs: Any,
    ) -> str: ...
