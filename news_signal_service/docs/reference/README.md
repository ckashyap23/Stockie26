# Reference material (not used at runtime)

Kept from the original design folder `news_signal_design/` (removed 2026-11-09). Nothing in `app/` reads these files, and the Docker
image does not include `docs/`.

| File | What it is | Status |
|---|---|---|
| `News_Signal_Source_and_Event_Catalogue.xlsx` | The original design workbook: Read me, News publishers (24), Primary sources, Event seeds (55), Context features, Access evidence, Input audit | Reference. The `Primary sources` and `Context features` sheets describe numeric/primary inputs that are not built yet. |
| `source_registry_original_24_publishers.csv` | The original 24 publishers with access/rights notes | Superseded by `data/feeds.yaml` (18 publishers). It still holds the rights notes for the dropped publishers (NDTV Profit, Informist, PTI, Reuters and AP direct, Argus, S&P Global Energy, ET Now, WSJ direct). |
| `event_seeds_original_55.csv` | The 55 seed events as first designed | Superseded by `data/event_seeds.csv` (same IDs, plus Judge, LLM guidance, Shock group, cues, context terms). |

The live, editable versions are in `data/`.
