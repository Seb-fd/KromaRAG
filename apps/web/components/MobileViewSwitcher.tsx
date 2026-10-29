"use client";

import { FileText, MessageSquare } from "lucide-react";
import { useStore } from "@/lib/store";
import { Button } from "./ui/button";
import { cn } from "@/lib/utils";

/** Mobile Chats/Docs switcher — shared by the chat header and the docs
    header so neither main-panel view is a dead end on small screens. */
export function MobileViewSwitcher({ className }: { className?: string }) {
  const { mainView, setMainView } = useStore();
  return (
    <div
      className={cn(
        "flex rounded-lg border border-[var(--border)] p-0.5 md:hidden",
        className,
      )}
      role="tablist"
      aria-label="Main view"
    >
      {(["chat", "docs"] as const).map((t) => (
        <Button
          key={t}
          variant="ghost"
          size="xs"
          role="tab"
          aria-selected={mainView === t}
          onClick={() => setMainView(t)}
          className={cn(
            "min-h-[36px]",
            t === mainView && "bg-kroma-500/15 text-current",
          )}
        >
          {t === "chat" ? (
            <MessageSquare aria-hidden />
          ) : (
            <FileText aria-hidden />
          )}
          <span className="capitalize">{t === "chat" ? "Chats" : "Docs"}</span>
        </Button>
      ))}
    </div>
  );
}
