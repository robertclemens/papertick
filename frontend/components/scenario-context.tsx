"use client";

import {
  createContext,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import { api, DeletedScenarioT, ScenarioT } from "@/lib/api";

/** The scenario lists, shared by everything that shows them.
 *
 *  The sidebar switcher and the scenarios page were each fetching their own
 *  copy, so creating or deleting a scenario in one left the other showing a
 *  stale list until a reload. One source, one refresh. */
interface ScenarioState {
  scenarios: ScenarioT[] | null;
  deleted: DeletedScenarioT[] | null;
  refresh: () => Promise<void>;
}

const ScenarioContext = createContext<ScenarioState>({
  scenarios: null,
  deleted: null,
  refresh: async () => {},
});

export function ScenarioProvider({ children }: { children: ReactNode }) {
  const [scenarios, setScenarios] = useState<ScenarioT[] | null>(null);
  const [deleted, setDeleted] = useState<DeletedScenarioT[] | null>(null);

  const refresh = useCallback(async () => {
    const [live, gone] = await Promise.all([
      api<ScenarioT[]>("/scenarios").catch(() => [] as ScenarioT[]),
      api<DeletedScenarioT[]>("/scenarios/deleted").catch(() => [] as DeletedScenarioT[]),
    ]);
    setScenarios(live);
    setDeleted(gone);
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  return (
    <ScenarioContext.Provider value={{ scenarios, deleted, refresh }}>
      {children}
    </ScenarioContext.Provider>
  );
}

export function useScenarios(): ScenarioState {
  return useContext(ScenarioContext);
}
