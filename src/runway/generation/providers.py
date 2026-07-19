from __future__ import annotations

import asyncio
import hashlib
from typing import Protocol

from PIL import Image, ImageDraw, ImageEnhance

from runway.generation.schemas import (
    ProviderCapabilities,
    ProviderImage,
    ProviderRequest,
)


class ImageGenerationProvider(Protocol):
    name: str
    model: str
    model_version: str
    capabilities: ProviderCapabilities

    async def generate(self, request: ProviderRequest) -> list[ProviderImage]: ...


class DeterministicMockImageProvider:
    """Offline fixture provider. It never downloads a model or invokes paid usage."""

    name = "mock"
    model = "deterministic-canvas"
    model_version = "1"
    capabilities = ProviderCapabilities(
        capabilities={"text_to_image", "reference_edit", "variation"},
        maximum_references=4,
        supports_seed=True,
        local_only=True,
        paid_usage=False,
    )

    async def generate(self, request: ProviderRequest) -> list[ProviderImage]:
        return await asyncio.to_thread(self._render, request)

    def _render(self, request: ProviderRequest) -> list[ProviderImage]:
        request.output_directory.mkdir(parents=True, exist_ok=True)
        outputs: list[ProviderImage] = []
        for index in range(request.output_count):
            seed = request.brief.seed
            if seed is None:
                identity = (
                    f"{request.run_identity}|{request.brief.instruction}|"
                    f"{request.brief.negative_instruction}|{index}"
                )
                seed = int(hashlib.sha256(identity.encode()).hexdigest()[:8], 16)
            else:
                seed += index
            digest = hashlib.sha256(
                f"{seed}|{request.brief.instruction}".encode()
            ).digest()
            if request.reference_paths:
                with Image.open(request.reference_paths[0]) as opened:
                    image = opened.convert("RGB").resize(
                        (request.brief.width, request.brief.height),
                        Image.Resampling.LANCZOS,
                    )
                image = ImageEnhance.Color(image).enhance(0.82 + digest[0] / 1024)
                overlay = Image.new(
                    "RGB",
                    image.size,
                    (digest[1], digest[2], digest[3]),
                )
                image = Image.blend(image, overlay, 0.08)
            else:
                image = Image.new(
                    "RGB",
                    (request.brief.width, request.brief.height),
                    (digest[0], digest[1], digest[2]),
                )
                draw = ImageDraw.Draw(image)
                for band in range(8):
                    inset = band * max(12, min(image.size) // 32)
                    color = (
                        digest[(band * 3 + 3) % len(digest)],
                        digest[(band * 3 + 4) % len(digest)],
                        digest[(band * 3 + 5) % len(digest)],
                    )
                    draw.rounded_rectangle(
                        (
                            inset,
                            inset,
                            image.width - inset - 1,
                            image.height - inset - 1,
                        ),
                        radius=max(8, inset // 3),
                        outline=color,
                        width=max(4, image.width // 128),
                    )
            output = request.output_directory / f"{request.run_identity}-{index}.png"
            image.save(output, format="PNG", optimize=True)
            outputs.append(
                ProviderImage(
                    path=output,
                    seed=seed,
                    provider_metadata={
                        "fixture": True,
                        "index": index,
                        "instruction_hash": hashlib.sha256(
                            request.brief.instruction.encode()
                        ).hexdigest(),
                    },
                    safety_result={
                        "provider_safe": True,
                        "reviewed_by": "deterministic_mock",
                    },
                )
            )
        return outputs


class ImageGenerationProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ImageGenerationProvider] = {
            "mock": DeterministicMockImageProvider(),
        }

    def get(self, name: str) -> ImageGenerationProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise LookupError(
                f"image-generation provider {name!r} is not registered; "
                "RunWay will not fall back implicitly"
            ) from exc

    def status(self) -> list[dict[str, object]]:
        return [
            {
                "name": provider.name,
                "model": provider.model,
                "model_version": provider.model_version,
                **provider.capabilities.model_dump(),
            }
            for provider in self._providers.values()
        ]
