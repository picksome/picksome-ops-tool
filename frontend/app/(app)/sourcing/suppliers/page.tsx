"use client";

/**
 * 공급처 관리 (M037).
 *
 * 왜 공급처가 Master 인가
 * --------------------
 * 실제 Excel 의 "구매처" 열은 URL 358종이었고 도메인은 소수였다
 * (도매꾹·천유·오너클랜·토비즈온). 이름 문자열로 관계를 유지하면 이름을 바꾸는
 * 순간 연결이 끊긴다. 그래서 `supplier_code` 라는 고정 코드를 둔다.
 *
 * 비밀번호는 화면에 없다
 * ------------------
 * 공급처 로그인 계정이 있다는 **사실만** 표시한다. 실제 secret 은 vault 가
 * 보관하고 여기로 내려오지 않는다 — DB 에 그 컬럼 자체가 없다.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ExternalLink, KeyRound, Plus, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { PageShell, Section } from "@/components/shell/page-shell";
import { ApiError } from "@/lib/api";
import {
  SUPPLIER_STATUS_LABEL, SUPPLIER_TYPE_LABEL, sourcingApi,
  type SupplierSite, type SupplierType,
} from "@/lib/api/sourcing";

function errorMessage(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) return `${action} 중 통신에 실패했습니다.`;
  if (err.status === 403) return `${action} 권한이 없습니다.`;
  if (err.status === 409) return err.message || "이미 있는 공급처입니다.";
  return err.message || `${action}에 실패했습니다.`;
}

export default function SuppliersPage() {
  const qc = useQueryClient();
  const [q, setQ] = React.useState("");
  const [open, setOpen] = React.useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["supplier-sites", q],
    queryFn: () => sourcingApi.listSuppliers({ q: q || undefined, limit: 200 }),
  });

  const suppliers = data?.items ?? [];

  return (
    <PageShell
      title="공급처"
      description="상품을 어디서 사 오는지 관리합니다. 이름을 바꿔도 연결이 유지되도록 코드로 식별합니다"
      actions={
        <Button size="sm" onClick={() => setOpen(true)}>
          <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          공급처 추가
        </Button>
      }
      variant="detail"
    >
      <div className="mb-3 flex items-center gap-2">
        <div className="relative">
          <Search
            className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[--color-muted-foreground]"
            aria-hidden
          />
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="이름 · 코드 · 도메인"
            aria-label="공급처 검색"
            className="h-8 w-64 pl-7 text-sm"
          />
        </div>
      </div>

      <Section title={`공급처 ${data?.total ?? 0}곳`}>
        {isLoading ? (
          <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
        ) : suppliers.length === 0 ? (
          <div className="py-8 text-center text-sm text-[--color-muted-foreground]">
            <p>등록된 공급처가 없습니다.</p>
            <p className="mt-1 text-xs">
              기존 상품의 구매 링크는 자동으로 공급처가 되지 않습니다 —
              링크만 보고 거래처를 만들면 근거 없는 정보가 생깁니다.
            </p>
          </div>
        ) : (
          <div className="ops-scroll-x">
            <table className="w-full text-[12px]">
              <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
                <tr>
                  <th scope="col" className="px-2 py-1.5 text-left font-semibold">코드</th>
                  <th scope="col" className="px-2 py-1.5 text-left font-semibold">공급처명</th>
                  <th scope="col" className="px-2 py-1.5 text-left font-semibold">유형</th>
                  <th scope="col" className="px-2 py-1.5 text-left font-semibold">국가</th>
                  <th scope="col" className="px-2 py-1.5 text-left font-semibold">기본통화</th>
                  <th scope="col" className="px-2 py-1.5 text-left font-semibold">사이트</th>
                  <th scope="col" className="px-2 py-1.5 text-center font-semibold">상태</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-semibold">연결 상품</th>
                  <th scope="col" className="px-2 py-1.5 text-center font-semibold">계정</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[--color-border]">
                {suppliers.map((s) => (
                  <SupplierRow key={s.id} supplier={s} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <CreateSupplierDialog
        open={open}
        onOpenChange={setOpen}
        onCreated={() => qc.invalidateQueries({ queryKey: ["supplier-sites"] })}
      />
    </PageShell>
  );
}

function SupplierRow({ supplier: s }: { supplier: SupplierSite }) {
  return (
    <tr>
      <td className="whitespace-nowrap px-2 py-1.5 font-mono text-[11px] text-[--color-muted-foreground]">
        {s.supplier_code}
      </td>
      <td className="px-2 py-1.5 font-medium">{s.name}</td>
      <td className="px-2 py-1.5">{SUPPLIER_TYPE_LABEL[s.supplier_type] ?? s.supplier_type}</td>
      <td className="px-2 py-1.5">{s.country_code ?? "—"}</td>
      <td className="px-2 py-1.5 tabular-nums">{s.default_currency}</td>
      <td className="px-2 py-1.5">
        {s.website_url ? (
          <a
            href={s.website_url}
            target="_blank"
            rel="noreferrer noopener"
            className="ops-focusable inline-flex items-center gap-1 underline underline-offset-2"
          >
            {s.website_domain ?? "열기"}
            <ExternalLink className="h-3 w-3" aria-hidden />
          </a>
        ) : (
          <span className="text-[--color-muted-foreground]">—</span>
        )}
      </td>
      <td className="px-2 py-1.5 text-center">
        <Badge variant={s.status === "ACTIVE" ? "default" : "warning"} className="px-1.5 py-0 text-[10px]">
          {SUPPLIER_STATUS_LABEL[s.status] ?? s.status}
        </Badge>
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums">{s.linked_product_count}</td>
      <td className="px-2 py-1.5 text-center">
        {/* 계정이 있다는 사실만. 비밀번호는 내려오지 않는다. */}
        {s.credential_present ? (
          <KeyRound
            className="mx-auto h-3.5 w-3.5 text-[--color-muted-foreground]"
            aria-label="로그인 계정 있음 (비밀번호는 저장하지 않습니다)"
          />
        ) : (
          <span className="text-[--color-muted-foreground]">—</span>
        )}
      </td>
    </tr>
  );
}

function CreateSupplierDialog({
  open, onOpenChange, onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: () => void;
}) {
  const [name, setName] = React.useState("");
  const [type, setType] = React.useState<SupplierType>("WHOLESALE");
  const [country, setCountry] = React.useState("");
  const [url, setUrl] = React.useState("");
  const [currency, setCurrency] = React.useState("KRW");
  const [credential, setCredential] = React.useState(false);

  const create = useMutation({
    mutationFn: () =>
      sourcingApi.createSupplier({
        name: name.trim(),
        supplier_type: type,
        country_code: country.trim().toUpperCase() || null,
        website_url: url.trim() || null,
        default_currency: currency.trim().toUpperCase() || "KRW",
        credential_present: credential,
      }),
    onSuccess: (s) => {
      onCreated();
      onOpenChange(false);
      setName(""); setCountry(""); setUrl(""); setCredential(false);
      toast.success(`${s.supplier_code} 공급처를 등록했습니다.`);
    },
    onError: (e) => toast.error(errorMessage(e, "공급처 등록")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>공급처 추가</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 py-2 text-sm">
          <div className="space-y-1">
            <Label htmlFor="sup-name" className="text-xs">공급처명 *</Label>
            <Input
              id="sup-name" value={name} onChange={(e) => setName(e.target.value)}
              className="h-8 text-sm" placeholder="도매꾹"
            />
            <p className="text-[11px] text-[--color-muted-foreground]">
              코드는 자동으로 부여됩니다. 이름을 바꿔도 연결은 유지됩니다.
            </p>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div className="space-y-1">
              <Label htmlFor="sup-type" className="text-xs">유형</Label>
              <select
                id="sup-type" value={type}
                onChange={(e) => setType(e.target.value as SupplierType)}
                className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
              >
                {Object.entries(SUPPLIER_TYPE_LABEL).map(([v, l]) => (
                  <option key={v} value={v}>{l}</option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="sup-country" className="text-xs">국가</Label>
              <Input
                id="sup-country" value={country} maxLength={2}
                onChange={(e) => setCountry(e.target.value)}
                className="h-8 text-sm" placeholder="KR"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="sup-cur" className="text-xs">기본통화</Label>
              <Input
                id="sup-cur" value={currency} maxLength={3}
                onChange={(e) => setCurrency(e.target.value)}
                className="h-8 text-sm"
              />
            </div>
          </div>
          <div className="space-y-1">
            <Label htmlFor="sup-url" className="text-xs">사이트 주소</Label>
            <Input
              id="sup-url" value={url} onChange={(e) => setUrl(e.target.value)}
              className="h-8 text-sm" placeholder="https://domeggook.com"
            />
          </div>
          <label className="flex items-start gap-2 text-xs">
            <input
              type="checkbox" checked={credential} className="ops-focusable mt-0.5"
              onChange={(e) => setCredential(e.target.checked)}
            />
            <span>
              로그인 계정이 있습니다
              <span className="block text-[11px] text-[--color-muted-foreground]">
                비밀번호는 여기에 저장하지 않습니다. 계정이 있다는 사실만 기록합니다.
              </span>
            </span>
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            취소
          </Button>
          <Button
            size="sm" disabled={!name.trim() || create.isPending}
            onClick={() => create.mutate()}
          >
            등록
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
