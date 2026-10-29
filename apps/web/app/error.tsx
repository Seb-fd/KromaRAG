"use client";

import { useEffect } from "react";

export default function Error({
  error,
  retry,
}: {
  error: unknown;
  retry: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-lg flex-col items-center justify-center gap-3 px-6 text-center">
      <p className="font-display text-sm tracking-widest uppercase ink-faint">Something went wrong</p>
      <h1 className="font-display text-2xl font-semibold tracking-tight">Couldn&apos;t load this view</h1>
      <p className="text-sm ink-soft">
        Try again — your chats and documents are safe.
      </p>
      <button
        type="button"
        onClick={() => retry()}
        className="kroma-gradient-btn mt-2 inline-flex h-10 items-center rounded-lg px-4 text-sm font-medium"
      >
        Try again
      </button>
    </main>
  );
}
