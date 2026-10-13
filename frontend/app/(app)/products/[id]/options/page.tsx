"use client";

import { use, useState, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import {
  ArrowLeft, Plus, Pencil, Trash2, Check, X, GripVertical,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import {
  productsApi,
  type OptionGroupWithValues,
  type ProductVariant,
} from "@/lib/api/products";

interface PageProps {
  params: Promise<{ id: string }>;
}

/* ─── 인라인 편집 셀 ─── */
function EditableCell({
  value,
  onSave,
  suffix,
  className,
}: {
  value: number | null;
  onSave: (v: number | null) => void;
  suffix?: string;
  className?: string;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const ref = useRef<HTMLInputElement>(null);

  const start = () => {
    setDraft(value != null ? String(value) : "");
    setEditing(true);
    setTimeout(() => ref.current?.select(), 0);
  };

  const commit = () => {
    const n = draft === "" ? null : parseInt(draft);
    if (n !== value) onSave(isNaN(n as number) ? null : n);
    setEditing(false);
  };

  if (editing) {
    return (
      <div className="flex items-center gap-1">
        <input
          ref={ref}
          type="number"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") setEditing(false);
          }}
          className="h-7 w-full rounded border border-[--color-accent] bg-white px-2 text-right tabular-nums text-xs outline-none"
        />
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={start}
      className={cn(
        "group flex h-7 w-full items-center justify-end gap-1 rounded px-2 text-xs tabular-nums transition-colors hover:bg-[--color-muted]",
        className,
      )}
    >
      <span>{value != null ? value.toLocaleString("ko-KR") : <span className="text-[--color-muted-foreground]">—</span>}</span>
      {suffix && <span className="text-[--color-muted-foreground]">{suffix}</span>}
      <Pencil className="h-2.5 w-2.5 shrink-0 text-[--color-muted-foreground] opacity-0 group-hover:opacity-100" />
    </button>
  );
}

/* ─── 옵션 그룹 패널 ─── */
function OptionGroupPanel({
  productId,
  groups,
}: {
  productId: string;
  groups: OptionGroupWithValues[];
}) {
  const qc = useQueryClient();
  const [addingGroup, setAddingGroup] = useState(false);
  const [newGroupName, setNewGroupName] = useState("");
  const [addingValueFor, setAddingValueFor] = useState<string | null>(null);
  const [newValue, setNewValue] = useState("");
  const [editingGroup, setEditingGroup] = useState<{ id: string; name: string } | null>(null);

  const createGroupMutation = useMutation({
    mutationFn: (name: string) =>
      productsApi.createOptionGroup(productId, { name, sort_order: groups.length }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["option-groups", productId] });
      toast.success("옵션 그룹 추가됨");
      setAddingGroup(false);
      setNewGroupName("");
    },
    onError: () => toast.error("추가에 실패했습니다."),
  });

  const updateGroupMutation = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      productsApi.updateOptionGroup(id, { name }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["option-groups", productId] });
      setEditingGroup(null);
    },
    onError: () => toast.error("수정에 실패했습니다."),
  });

  const deleteGroupMutation = useMutation({
    mutationFn: (gid: string) => productsApi.deleteOptionGroup(gid),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["option-groups", productId] });
      qc.invalidateQueries({ queryKey: ["variants", productId] });
      toast.success("옵션 그룹 삭제됨");
    },
    onError: () => toast.error("삭제 실패 — variant에서 사용 중일 수 있습니다."),
  });

  const createValueMutation = useMutation({
    mutationFn: ({ gid, value }: { gid: string; value: string }) =>
      productsApi.createOptionValue(gid, { value }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["option-groups", productId] });
      qc.invalidateQueries({ queryKey: ["variants", productId] });
      toast.success("옵션 값 추가됨");
      setAddingValueFor(null);
      setNewValue("");
    },
    onError: () => toast.error("추가에 실패했습니다."),
  });

  const deleteValueMutation = useMutation({
    mutationFn: ({ vid }: { gid: string; vid: string }) =>
      productsApi.deleteOptionValue(vid),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["option-groups", productId] });
      qc.invalidateQueries({ queryKey: ["variants", productId] });
      toast.success("옵션 값 삭제됨");
    },
    onError: () => toast.error("삭제 실패 — variant에서 사용 중일 수 있습니다."),
  });

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-sm font-semibold">옵션 그룹</p>
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs"
          onClick={() => setAddingGroup(true)}
        >
          <Plus className="mr-1 h-3 w-3" />
          그룹 추가
        </Button>
      </div>

      {groups.length === 0 && !addingGroup && (
        <p className="rounded-lg border border-dashed border-[--color-border] py-6 text-center text-sm text-[--color-muted-foreground]">
          옵션 그룹이 없습니다. (예: 색상, 사이즈)
        </p>
      )}

      <div className="space-y-2">
        {groups.map((group) => (
          <div key={group.id} className="rounded-lg border border-[--color-border] bg-[--color-card] p-3">
            {/* 그룹 헤더 */}
            <div className="flex items-center gap-2">
              <GripVertical className="h-4 w-4 shrink-0 text-[--color-muted-foreground]" />
              {editingGroup?.id === group.id ? (
                <div className="flex flex-1 items-center gap-1">
                  <Input
                    className="h-7 text-xs"
                    value={editingGroup.name}
                    onChange={(e) => setEditingGroup({ ...editingGroup, name: e.target.value })}
                    onKeyDown={(e) => {
                      if (e.key === "Enter")
                        updateGroupMutation.mutate({ id: group.id, name: editingGroup.name });
                      if (e.key === "Escape") setEditingGroup(null);
                    }}
                    autoFocus
                  />
                  <Button
                    size="sm"
                    className="h-7 w-7 p-0"
                    onClick={() =>
                      updateGroupMutation.mutate({ id: group.id, name: editingGroup.name })
                    }
                  >
                    <Check className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 w-7 p-0"
                    onClick={() => setEditingGroup(null)}
                  >
                    <X className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ) : (
                <>
                  <span className="flex-1 text-sm font-medium">{group.name}</span>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 w-6 p-0"
                    onClick={() => setEditingGroup({ id: group.id, name: group.name })}
                  >
                    <Pencil className="h-3 w-3" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 w-6 p-0 text-[--color-muted-foreground] hover:text-red-500"
                    onClick={() => {
                      if (confirm(`"${group.name}" 그룹을 삭제하시겠습니까?`))
                        deleteGroupMutation.mutate(group.id);
                    }}
                  >
                    <Trash2 className="h-3 w-3" />
                  </Button>
                </>
              )}
            </div>

            {/* 옵션 값 칩 */}
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              {group.values.map((val) => (
                <span
                  key={val.id}
                  className="group flex items-center gap-1 rounded-full border border-[--color-border] bg-[--color-surface-muted] px-2.5 py-0.5 text-xs"
                >
                  {val.value}
                  <button
                    type="button"
                    className="text-[--color-muted-foreground] opacity-0 transition-opacity group-hover:opacity-100 hover:text-red-500"
                    onClick={() => deleteValueMutation.mutate({ gid: group.id, vid: val.id })}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}

              {addingValueFor === group.id ? (
                <div className="flex items-center gap-1">
                  <Input
                    className="h-7 w-24 text-xs"
                    placeholder="값 입력"
                    value={newValue}
                    onChange={(e) => setNewValue(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && newValue.trim())
                        createValueMutation.mutate({ gid: group.id, value: newValue.trim() });
                      if (e.key === "Escape") { setAddingValueFor(null); setNewValue(""); }
                    }}
                    autoFocus
                  />
                  <Button
                    size="sm"
                    className="h-7 w-7 p-0"
                    disabled={!newValue.trim()}
                    onClick={() => createValueMutation.mutate({ gid: group.id, value: newValue.trim() })}
                  >
                    <Check className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 w-7 p-0"
                    onClick={() => { setAddingValueFor(null); setNewValue(""); }}
                  >
                    <X className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => { setAddingValueFor(group.id); setNewValue(""); }}
                  className="flex items-center gap-1 rounded-full border border-dashed border-[--color-border] px-2.5 py-0.5 text-xs text-[--color-muted-foreground] hover:border-[--color-accent] hover:text-[--color-accent]"
                >
                  <Plus className="h-3 w-3" />
                  값 추가
                </button>
              )}
            </div>
          </div>
        ))}

        {/* 새 그룹 추가 인풋 */}
        {addingGroup && (
          <div className="flex items-center gap-2 rounded-lg border border-[--color-accent]/40 bg-[--color-info-bg] p-3">
            <Input
              className="h-7 flex-1 text-xs"
              placeholder="그룹명 (예: 색상, 사이즈)"
              value={newGroupName}
              onChange={(e) => setNewGroupName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && newGroupName.trim())
                  createGroupMutation.mutate(newGroupName.trim());
                if (e.key === "Escape") { setAddingGroup(false); setNewGroupName(""); }
              }}
              autoFocus
            />
            <Button
              size="sm"
              className="h-7"
              disabled={!newGroupName.trim()}
              onClick={() => createGroupMutation.mutate(newGroupName.trim())}
            >
              추가
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 w-7 p-0"
              onClick={() => { setAddingGroup(false); setNewGroupName(""); }}
            >
              <X className="h-4 w-4" />
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

/* ─── Variant 그리드 ─── */
function VariantGrid({
  productId,
  variants,
  groups,
}: {
  productId: string;
  variants: ProductVariant[];
  groups: OptionGroupWithValues[];
}) {
  const qc = useQueryClient();

  const updateMutation = useMutation({
    mutationFn: ({
      vid,
      field,
      value,
    }: {
      vid: string;
      field: keyof Pick<
        ProductVariant, "cost_amount" | "sale_price" | "extra_price" | "stock_quantity"
      >;
      value: number | null;
    }) => productsApi.updateVariant(vid, { [field]: value }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["variants", productId] });
    },
    onError: () => toast.error("저장에 실패했습니다."),
  });

  const deleteMutation = useMutation({
    mutationFn: (vid: string) => productsApi.deleteVariant(vid),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["variants", productId] });
      toast.success("Variant 삭제됨");
    },
    onError: () => toast.error("삭제 실패 — 판매 기록에 연결된 variant입니다."),
  });

  if (variants.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-[--color-border] py-8 text-center text-sm text-[--color-muted-foreground]">
        Variant가 없습니다. 옵션 값을 추가하면 자동 생성됩니다.
      </p>
    );
  }

  const groupNames = groups.map((g) => g.name);

  return (
    <div className="overflow-x-auto rounded-xl border border-[--color-border]">
      <table className="w-full text-sm">
        <thead className="bg-[--color-surface-muted] text-xs text-[--color-muted-foreground]">
          <tr>
            {groupNames.map((name) => (
              <th key={name} className="px-3 py-2 text-left font-medium">
                {name}
              </th>
            ))}
            <th className="px-3 py-2 text-right font-medium">원가</th>
            <th className="px-3 py-2 text-right font-medium">판매가</th>
            <th className="px-3 py-2 text-right font-medium">추가금</th>
            <th className="px-3 py-2 text-right font-medium">재고</th>
            <th className="px-3 py-2 text-center font-medium">활성</th>
            <th className="w-8" />
          </tr>
        </thead>
        <tbody className="divide-y divide-[--color-border]">
          {variants.map((v) => (
            <tr key={v.id} className="hover:bg-[--color-muted]/30 transition-colors">
              {groupNames.map((name) => (
                <td key={name} className="px-3 py-1.5">
                  <span className="rounded-full bg-[--color-surface-muted] px-2 py-0.5 text-xs">
                    {v.option_values_json[name] ?? "—"}
                  </span>
                </td>
              ))}
              <td className="px-1 py-1">
                <EditableCell
                  value={v.cost_amount}
                  suffix="원"
                  onSave={(val) =>
                    updateMutation.mutate({ vid: v.id, field: "cost_amount", value: val })
                  }
                />
              </td>
              <td className="px-1 py-1">
                <EditableCell
                  value={v.sale_price}
                  suffix="원"
                  onSave={(val) =>
                    updateMutation.mutate({ vid: v.id, field: "sale_price", value: val })
                  }
                />
              </td>
              <td className="px-1 py-1">
                <EditableCell
                  value={v.extra_price}
                  suffix="원"
                  onSave={(val) =>
                    updateMutation.mutate({ vid: v.id, field: "extra_price", value: val ?? 0 })
                  }
                />
              </td>
              <td className="px-1 py-1">
                <EditableCell
                  value={v.stock_quantity}
                  suffix="개"
                  onSave={(val) =>
                    updateMutation.mutate({ vid: v.id, field: "stock_quantity", value: val })
                  }
                />
              </td>
              <td className="px-3 py-1.5 text-center">
                <input
                  type="checkbox"
                  checked={v.is_active}
                  onChange={(e) =>
                    productsApi
                      .updateVariant(v.id, { is_active: e.target.checked })
                      .then(() => qc.invalidateQueries({ queryKey: ["variants", productId] }))
                  }
                  className="rounded"
                />
              </td>
              <td className="px-2 py-1.5">
                <button
                  type="button"
                  className="text-[--color-muted-foreground] hover:text-red-500 transition-colors"
                  onClick={() => {
                    if (confirm("이 variant를 삭제하시겠습니까?"))
                      deleteMutation.mutate(v.id);
                  }}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ─── 메인 페이지 ─── */
export default function ProductOptionsPage({ params }: PageProps) {
  const { id: productId } = use(params);
  const router = useRouter();

  const { data: product } = useQuery({
    queryKey: ["product", productId],
    queryFn: () => productsApi.get(productId),
  });

  const { data: groups = [], isLoading: groupsLoading } = useQuery({
    queryKey: ["option-groups", productId],
    // 값까지 필요하다 — 서버가 nested 로 주지 않으므로 합쳐 주는 헬퍼를 쓴다.
    queryFn: () => productsApi.getOptionGroupsWithValues(productId),
  });

  const { data: variants = [], isLoading: variantsLoading } = useQuery({
    queryKey: ["variants", productId],
    queryFn: () => productsApi.getVariants(productId),
  });

  const totalStock = variants.reduce((s, v) => s + (v.stock_quantity ?? 0), 0);
  const activeCount = variants.filter((v) => v.is_active).length;

  return (
    <div className="mx-auto max-w-4xl space-y-6 p-6">
      {/* 헤더 */}
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => router.back()}>
          <ArrowLeft className="mr-1 h-4 w-4" />
          상품
        </Button>
        <div className="flex-1">
          <h1 className="text-xl font-semibold">
            {product?.name_ko ?? "옵션/Variant 관리"}
          </h1>
          <p className="text-sm text-[--color-muted-foreground]">
            옵션 그룹 설정 → Variant 가격/재고 입력
          </p>
        </div>
        {variants.length > 0 && (
          <div className="flex gap-4 text-right text-xs text-[--color-muted-foreground]">
            <div>
              <p className="tabular-nums text-base font-semibold text-[--color-foreground]">
                {activeCount}
              </p>
              <p>활성 variant</p>
            </div>
            <div>
              <p className="tabular-nums text-base font-semibold text-[--color-foreground]">
                {totalStock.toLocaleString("ko-KR")}
              </p>
              <p>총 재고</p>
            </div>
          </div>
        )}
      </div>

      {/* 옵션 그룹 패널 */}
      {groupsLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 2 }).map((_, i) => (
            <div key={i} className="h-20 animate-pulse rounded-lg bg-[--color-muted]" />
          ))}
        </div>
      ) : (
        <OptionGroupPanel productId={productId} groups={groups} />
      )}

      <Separator />

      {/* Variant 그리드 */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <p className="text-sm font-semibold">
            Variant 그리드
            {variants.length > 0 && (
              <span className="ml-2 text-xs font-normal text-[--color-muted-foreground]">
                {variants.length}개 — 클릭하여 인라인 편집
              </span>
            )}
          </p>
        </div>
        {variantsLoading ? (
          <div className="h-32 animate-pulse rounded-xl bg-[--color-muted]" />
        ) : (
          <VariantGrid productId={productId} variants={variants} groups={groups} />
        )}
      </div>
    </div>
  );
}
