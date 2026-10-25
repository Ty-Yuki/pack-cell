"""品物カタログ(config/catalog.yaml)の読み込み。"""
import pathlib
from dataclasses import dataclass, field

import yaml

DEFAULT_PATH = pathlib.Path(__file__).resolve().parents[3] / "config" / "catalog.yaml"


@dataclass(frozen=True)
class Sku:
    id: str
    name: str
    aliases: tuple
    size: tuple            # (W, D, H) [m]。W が長辺
    mass: float
    max_load: float
    temp: str
    tags: tuple
    food: bool
    color: tuple
    shape: str
    label_color: tuple

    @property
    def round(self):
        """円柱(缶・カップ): 水平の向きは関係ない。"""
        return self.shape in ("can", "cup")

    @property
    def height(self):
        return self.size[2]


@dataclass
class Catalog:
    skus: dict = field(default_factory=dict)

    def __getitem__(self, k):
        return self.skus[k]

    def __contains__(self, k):
        return k in self.skus

    def __iter__(self):
        return iter(self.skus.values())



def load(path=DEFAULT_PATH):
    raw = yaml.safe_load(pathlib.Path(path).read_text())
    skus = {}
    for k, v in raw.items():
        skus[k] = Sku(id=k, name=v["name"], aliases=tuple(v.get("aliases", [])),
                      size=tuple(c / 100.0 for c in v["size_cm"]), mass=float(v["mass_kg"]),
                      max_load=float(v["max_load_kg"]), temp=v["temp"], tags=tuple(v.get("tags", [])),
                      food=bool(v.get("food", True)), color=tuple(v.get("color", (0.8, 0.8, 0.8))),
                      shape=v.get("shape", "box"), label_color=tuple(v.get("label_color", (0.2, 0.2, 0.2))))
    return Catalog(skus)
