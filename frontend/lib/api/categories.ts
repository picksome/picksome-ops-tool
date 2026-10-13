import { api } from "@/lib/api";

export interface ExpenseCategory {
  id: string;
  name: string;
  code: string;
  description: string | null;
  is_deductible: boolean;
  parent_id: string | null;
  parent_name?: string;
  expense_count?: number;
}

export interface ExpenseCategoryCreate {
  name: string;
  code: string;
  description?: string | null;
  is_deductible?: boolean;
  parent_id?: string | null;
}

export const categoriesApi = {
  list: () => api.get<ExpenseCategory[]>("/expense-categories"),

  create: (body: ExpenseCategoryCreate) =>
    api.post<ExpenseCategory>("/expense-categories", body),

  update: (id: string, body: Partial<ExpenseCategoryCreate>) =>
    api.patch<ExpenseCategory>(`/expense-categories/${id}`, body),

  delete: (id: string) =>
    api.delete(`/expense-categories/${id}`),
};
