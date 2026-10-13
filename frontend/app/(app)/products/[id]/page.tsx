"use client";

import { use, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { DataTable } from "@/components/table/data-table";
import { TablePagination } from "@/components/table/table-pagination";
import { MoneyText } from "@/components/ui/finance";
import { PageHeader } from "@/components/shell/page-header";
import { api } from "@/lib/api";
import { productsApi, type Product, type ProductStatus, type MarginType } from "@/lib/api/products";
import { type SaleOrder } from "@/lib/api/sales";
import { useSaleChannels } from "@/hooks/useSaleChannels";
import { ColumnDef } from "@tanstack/react-table";
import { toast } from "sonner";
import { Pencil, ExternalLink } from "lucide-react";

// ─── Types ───────────────────────────────────────────────────────────────────

interface ExpectedMargin {
  id: string;
  marketplace: string;
  input_cost: number;
  input_sale_price: number;
  computed_margin_rate: number;
  computed_net_margin: number;
  created_at: string;
}

// ─── Badge helpers ────────────────────────────────────────────────────────────

const STATUS_VARIANT: Record<ProductStatus, "info" | "success" | "warning" | "default"> = {
  CANDIDATE: "info",
  APPROVED: "info",
  ACTIVE: "success",
  PAUSED: "warning",
  DISCONTINUED: "default",
};

const STATUS_LABEL: Record<string, string> = {
  CANDIDATE: "검토중",
  APPROVED: "승인됨",
  ACTIVE: "판매중",
  PAUSED: "일시중지",
  DISCONTINUED: "단종",
};

const MARGIN_VARIANT: Record<MarginType, "default" | "success" | "info" | "warning"> = {
  PURCHASE_AGENCY: "default",
  ROCKET_GROWTH: "success",
  OVERSEAS_DIRECT: "info",
  GENERAL_RESELL: "warning",
};

const MARGIN_LABEL: Record<MarginType, string> = {
  PURCHASE_AGENCY: "구매대행",
  ROCKET_GROWTH: "로켓그로스",
  OVERSEAS_DIRECT: "해외직구",
  GENERAL_RESELL: "일반재판매",
};

// ─── Column defs ──────────────────────────────────────────────────────────────

function buildMarginColumns(labelOf: (code: string) => string): ColumnDef<ExpectedMargin>[] {
  return [
  {
    accessorKey: "marketplace",
    header: "마켓",
    cell: ({ getValue }) => (
      <span className="text-sm">{labelOf(getValue<string>())}</span>
    ),
    size: 120,
  },
  {
    accessorKey: "input_cost",
    header: "원가",
    cell: ({ getValue }) => <MoneyText value={getValue<number>()} />,
    size: 110,
  },
  {
    accessorKey: "input_sale_price",
    header: "판매가",
    cell: ({ getValue }) => <MoneyText value={getValue<number>()} />,
    size: 110,
  },
  {
    accessorKey: "computed_margin_rate",
    header: "마진율",
    cell: ({ getValue }) => (
      <span className="tabular-nums text-sm">
        {(getValue<number>() * 100).toFixed(1)}%
      </span>
    ),
    size: 90,
  },
  {
    accessorKey: "created_at",
    header: "계산일",
    cell: ({ getValue }) => (
      <span className="text-xs text-[--color-muted-foreground]">
        {getValue<string>().slice(0, 10)}
      </span>
    ),
    size: 100,
  },
  ];
}

function buildSaleColumns(labelOf: (code: string) => string): ColumnDef<SaleOrder>[] {
  return [
  {
    accessorKey: "sale_date",
    header: "판매일",
    cell: ({ getValue }) => (
      <span className="text-xs tabular-nums">
        {getValue<string>().slice(5).replace("-", "/")}
      </span>
    ),
    size: 70,
  },
  {
    accessorKey: "marketplace",
    header: "마켓",
    cell: ({ getValue }) => (
      <Badge variant="info" className="text-[11px]">
        {labelOf(getValue<string>())}
      </Badge>
    ),
    size: 100,
  },
  {
    accessorKey: "quantity",
    header: "수량",
    cell: ({ getValue }) => (
      <span className="tabular-nums text-sm">{getValue<number>()}</span>
    ),
    size: 60,
  },
  {
    accessorKey: "sale_price",
    header: "판매가",
    cell: ({ getValue }) => <MoneyText value={getValue<number>()} />,
    size: 110,
  },
  {
    accessorKey: "ad_cost",
    header: "광고비",
    cell: ({ getValue }) => <MoneyText value={getValue<number>()} />,
    size: 100,
  },
  ];
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function ProductDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const queryClient = useQueryClient();
  const { labelOf } = useSaleChannels();

  // Sheet state
  const [sheetOpen, setSheetOpen] = useState(false);
  const [editName, setEditName] = useState("");
  const [editStatus, setEditStatus] = useState<ProductStatus>("ACTIVE");
  const [editMarginType, setEditMarginType] = useState<MarginType>("GENERAL_RESELL");
  const [editCost, setEditCost] = useState("");
  const [editMemo, setEditMemo] = useState("");

  // Sales tab pagination
  const [salesPage, setSalesPage] = useState(1);

  // ─── Queries ────────────────────────────────────────────────────────────────

  const { data: product, isLoading: productLoading } = useQuery({
    queryKey: ["product", id],
    queryFn: () => productsApi.get(id),
  });

  const { data: marginData, isLoading: marginLoading } = useQuery({
    queryKey: ["product-margins", id],
    queryFn: () =>
      api.get<{ items: ExpectedMargin[]; total: number }>("/margin/expected", {
        query: { product_id: id, page_size: 20 },
      }),
  });

  const { data: salesData, isLoading: salesLoading } = useQuery({
    queryKey: ["product-sales", id, salesPage],
    queryFn: () =>
      api.get<{ items: SaleOrder[]; total: number; page: number; page_size: number }>(
        "/sales",
        { query: { product_id: id, page: salesPage, page_size: 20 } },
      ),
  });

  // ─── Mutation ───────────────────────────────────────────────────────────────

  const updateMutation = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.patch<Product>(`/products/${id}`, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["product", id] });
      setSheetOpen(false);
      toast.success("상품 정보가 저장되었습니다.");
    },
    onError: () => toast.error("저장에 실패했습니다."),
  });

  // ─── Handlers ───────────────────────────────────────────────────────────────

  function openEditSheet() {
    if (!product) return;
    setEditName(product.name_ko);
    setEditStatus(product.status);
    setEditMarginType(product.margin_type);
    setEditCost(String(product.cost_amount ?? ""));
    setEditMemo((product as unknown as { memo?: string }).memo ?? "");
    setSheetOpen(true);
  }

  function handleSave() {
    if (!editName.trim()) {
      toast.error("상품명을 입력하세요.");
      return;
    }
    updateMutation.mutate({
      name_ko: editName.trim(),
      status: editStatus,
      margin_type: editMarginType,
      cost_amount: editCost ? Number(editCost) : null,
      memo: editMemo || null,
    });
  }

  // ─── Derived ────────────────────────────────────────────────────────────────

  const displayName = product?.name_ko ?? "";

  const salesItems = salesData?.items ?? [];
  const salesTotal = salesData?.total ?? 0;
  const salesPageSize = 20;

  const marginItems = marginData?.items ?? [];

  // ─── Render ──────────────────────────────────────────────────────────────────

  if (productLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <span className="text-sm text-[--color-muted-foreground]">로딩중…</span>
      </div>
    );
  }

  if (!product) {
    return (
      <div className="flex h-full items-center justify-center">
        <span className="text-sm text-[--color-muted-foreground]">상품을 찾을 수 없습니다.</span>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col gap-4 p-6">
      <PageHeader
        title={displayName || "상품 상세"}
        description={`코드: ${product.business_key}`}
        actions={
          <Button variant="outline" size="sm" onClick={openEditSheet}>
            <Pencil className="mr-1.5 h-3.5 w-3.5" />
            편집
          </Button>
        }
      />

      <Tabs defaultValue="info" className="flex flex-1 flex-col gap-4">
        <TabsList className="w-fit">
          <TabsTrigger value="info">기본정보</TabsTrigger>
          <TabsTrigger value="margin">마진</TabsTrigger>
          <TabsTrigger value="sales">판매이력</TabsTrigger>
        </TabsList>

        {/* ── Tab 1: 기본정보 ─────────────────────────────────────────── */}
        <TabsContent value="info" className="flex-1">
          <div className="rounded-lg border border-[--color-border] bg-[--color-card] p-6">
            <dl className="grid grid-cols-2 gap-x-8 gap-y-5 sm:grid-cols-3">
              <div>
                <dt className="text-xs font-medium text-[--color-muted-foreground]">상품 코드</dt>
                <dd className="mt-1 font-mono text-sm">{product.business_key}</dd>
              </div>
              <div>
                <dt className="text-xs font-medium text-[--color-muted-foreground]">상품명</dt>
                <dd className="mt-1 text-sm font-medium">{displayName}</dd>
              </div>
              <div>
                <dt className="text-xs font-medium text-[--color-muted-foreground]">상태</dt>
                <dd className="mt-1">
                  <Badge variant={STATUS_VARIANT[product.status as ProductStatus] ?? "default"}>
                    {STATUS_LABEL[product.status] ?? product.status}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt className="text-xs font-medium text-[--color-muted-foreground]">마진 유형</dt>
                <dd className="mt-1">
                  <Badge variant={MARGIN_VARIANT[product.margin_type] ?? "default"}>
                    {MARGIN_LABEL[product.margin_type] ?? product.margin_type}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt className="text-xs font-medium text-[--color-muted-foreground]">옵션</dt>
                <dd className="mt-1">
                  {product.has_variants ? (
                    <Badge variant="info" className="text-[11px]">옵션 있음</Badge>
                  ) : (
                    <span className="text-sm text-[--color-muted-foreground]">단일</span>
                  )}
                </dd>
              </div>
              <div>
                <dt className="text-xs font-medium text-[--color-muted-foreground]">원가</dt>
                <dd className="mt-1">
                  <MoneyText value={product.cost_amount} />
                  {product.cost_currency && product.cost_currency !== "KRW" && (
                    <span className="ml-1.5 text-xs text-[--color-muted-foreground]">
                      ({product.cost_amount_foreign} {product.cost_currency} × {product.cost_fx_rate})
                    </span>
                  )}
                </dd>
              </div>
              <div>
                <dt className="text-xs font-medium text-[--color-muted-foreground]">등록일</dt>
                <dd className="mt-1 text-sm tabular-nums">
                  {product.created_at.slice(0, 10)}
                </dd>
              </div>
              {(product as unknown as { memo?: string }).memo && (
                <div className="col-span-2 sm:col-span-3">
                  <dt className="text-xs font-medium text-[--color-muted-foreground]">메모</dt>
                  <dd className="mt-1 text-sm">
                    {(product as unknown as { memo?: string }).memo}
                  </dd>
                </div>
              )}
            </dl>

            <div className="mt-6 flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => router.push(`/products/${id}/options`)}
              >
                옵션 관리
              </Button>
            </div>
          </div>
        </TabsContent>

        {/* ── Tab 2: 마진 ──────────────────────────────────────────────── */}
        <TabsContent value="margin" className="flex flex-1 flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="text-sm text-[--color-muted-foreground]">
              이 상품의 마켓별 예상 마진 계산 이력입니다.
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => router.push("/margin/calculator")}
            >
              <ExternalLink className="mr-1.5 h-3.5 w-3.5" />
              마진 계산기로 이동
            </Button>
          </div>
          <div className="flex-1 overflow-hidden">
            <DataTable
              columns={buildMarginColumns(labelOf)}
              data={marginItems}
              isLoading={marginLoading}
              emptyText="마진 계산 이력이 없습니다"
            />
          </div>
        </TabsContent>

        {/* ── Tab 3: 판매이력 ──────────────────────────────────────────── */}
        <TabsContent value="sales" className="flex flex-1 flex-col gap-3">
          <div className="flex-1 overflow-hidden">
            <DataTable
              columns={buildSaleColumns(labelOf)}
              data={salesItems}
              isLoading={salesLoading}
              emptyText="판매 이력이 없습니다"
            />
          </div>
          <TablePagination
            page={salesPage}
            pageSize={salesPageSize}
            total={salesTotal}
            hasNextPage={salesPage * salesPageSize < salesTotal}
            onPageChange={setSalesPage}
            onPageSizeChange={() => {}}
            pageSizeOptions={[20]}
          />
        </TabsContent>
      </Tabs>

      {/* ── Edit Sheet ───────────────────────────────────────────────────── */}
      <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
        <SheetContent className="w-[420px] overflow-y-auto">
          <SheetHeader>
            <SheetTitle>상품 정보 편집</SheetTitle>
          </SheetHeader>

          <div className="mt-6 flex flex-col gap-5">
            {/* 상품명 */}
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="edit-name">
                상품명 <span className="text-[--color-danger]">*</span>
              </Label>
              <Input
                id="edit-name"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                placeholder="상품명을 입력하세요"
              />
            </div>

            {/* 상태 */}
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="edit-status">상태</Label>
              <Select
                value={editStatus}
                onValueChange={(v) => setEditStatus(v as ProductStatus)}
              >
                <SelectTrigger id="edit-status">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="CANDIDATE">검토중</SelectItem>
                  <SelectItem value="APPROVED">승인됨</SelectItem>
                  <SelectItem value="ACTIVE">판매중</SelectItem>
                  <SelectItem value="PAUSED">일시중지</SelectItem>
                  <SelectItem value="DISCONTINUED">단종</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {/* 마진 유형 */}
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="edit-margin-type">마진 유형</Label>
              <Select
                value={editMarginType}
                onValueChange={(v) => setEditMarginType(v as MarginType)}
              >
                <SelectTrigger id="edit-margin-type">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="PURCHASE_AGENCY">구매대행</SelectItem>
                  <SelectItem value="ROCKET_GROWTH">로켓그로스</SelectItem>
                  <SelectItem value="OVERSEAS_DIRECT">해외직구</SelectItem>
                  <SelectItem value="GENERAL_RESELL">일반재판매</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {/* 원가 */}
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="edit-cost">원가 (원)</Label>
              <Input
                id="edit-cost"
                type="number"
                value={editCost}
                onChange={(e) => setEditCost(e.target.value)}
                placeholder="0"
                min={0}
              />
            </div>

            {/* 메모 */}
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="edit-memo">메모</Label>
              <Textarea
                id="edit-memo"
                value={editMemo}
                onChange={(e) => setEditMemo(e.target.value)}
                placeholder="메모를 입력하세요"
                rows={3}
              />
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <Button variant="outline" onClick={() => setSheetOpen(false)}>
                취소
              </Button>
              <Button onClick={handleSave} disabled={updateMutation.isPending}>
                {updateMutation.isPending ? "저장 중…" : "저장"}
              </Button>
            </div>
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}
