import {
  Activity,
  BookOpen,
  Building2,
  FileText,
  Home,
  KeyRound,
  LayoutList,
  NotebookTabs,
  Package,
  Receipt,
  Repeat,
  Scale,
  ShieldCheck,
  Tag,
  Undo2,
  Users,
  UsersRound,
  Wallet,
  Webhook,
  Zap,
  type LucideIcon,
} from 'lucide-react';

/** Manifest icon names → the fixed icon set. Unknown names fall back safely. */
const ICONS: Record<string, LucideIcon> = {
  home: Home,
  users: Users,
  'users-group': UsersRound,
  building: Building2,
  shield: ShieldCheck,
  key: KeyRound,
  wallet: Wallet,
  repeat: Repeat,
  package: Package,
  tag: Tag,
  scale: Scale,
  ledger: NotebookTabs,
  receipt: Receipt,
  webhook: Webhook,
  zap: Zap,
  undo: Undo2,
  audit: FileText,
  health: Activity,
  help: BookOpen,
};

export function iconFor(name: string | null | undefined): LucideIcon {
  return (name ? ICONS[name] : undefined) ?? LayoutList;
}
