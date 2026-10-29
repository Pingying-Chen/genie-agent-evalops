// One tiny data-loading hook: { data, error, loading, reload } for any async call.
import { useCallback, useEffect, useState } from "react";

export function useLoad<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps);

  const reload = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    run()
      .then((value) => !cancelled && setData(value))
      .catch((err: Error) => !cancelled && setError(err))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true; // a late response from a previous render must not overwrite newer data
    };
  }, [run]);

  useEffect(() => reload(), [reload]);
  return { data, error, loading, reload };
}
