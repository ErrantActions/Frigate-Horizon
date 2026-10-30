import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { LuWrench } from "react-icons/lu";
import type { NavData } from "@/types/navigation";
import { cn } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "../ui/dropdown-menu";

type Props = {
  items: NavData[];
  className?: string;
  large?: boolean;
  variant?: "primary" | "secondary";
};

export default function ToolsMenu({
  items,
  className,
  large = false,
  variant = "primary",
}: Props) {
  const { t } = useTranslation("common");
  const { pathname } = useLocation();
  const available = items.filter((item) => item.enabled !== false);
  if (!available.length) return null;
  const active = available.some(
    (item) => pathname === item.url || pathname.startsWith(item.url + "/"),
  );
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={t("menu.tools")}
          title={t("menu.tools")}
          className={cn(
            "flex shrink-0 items-center justify-center rounded-lg p-2",
            large ? "size-12" : "size-11",
            variant === "primary"
              ? active
                ? "bg-selected text-white hover:bg-selected/80"
                : "bg-secondary text-secondary-foreground hover:bg-muted"
              : active
                ? "text-selected"
                : "text-secondary-foreground",
            className,
          )}
        >
          <LuWrench className={large ? "size-6" : "size-5"} aria-hidden />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        side={variant === "primary" ? "right" : "top"}
        align="start"
      >
        <DropdownMenuLabel>{t("menu.tools")}</DropdownMenuLabel>
        {available.map((item) => (
          <DropdownMenuItem key={item.id} asChild>
            <Link
              to={item.url}
              aria-current={pathname === item.url ? "page" : undefined}
              className="min-h-11 gap-2"
            >
              <item.icon className="size-4" aria-hidden />
              {t(item.title)}
            </Link>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
