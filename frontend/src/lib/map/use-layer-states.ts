"use client";

/**
 * Every rail card's `layerState` (`lib/layers.ts`) for the rail state, the
 * map's zoom and the published version on the map. The rail and the legend read it, so both say
 * the same thing about a layer the map cannot draw right now ("zoom in to see", "no data yet").
 *
 * The store selector returns one string (`layerStatesKey`): components re-render when a card's
 * state changes, not on every camera frame of a zoom.
 */
import { useMemo } from "react";

import type { TilesCurrent } from "@/lib/api/types";
import { layerStatesKey, parseLayerStates, type LayerId, type LayerState } from "@/lib/layers";
import { useShell } from "@/lib/store";

export function useLayerStates(tiles: TilesCurrent | null | undefined): Record<LayerId, LayerState> {
  const key = useShell((s) => layerStatesKey(s, tiles));
  return useMemo(() => parseLayerStates(key), [key]);
}
