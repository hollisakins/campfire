import { useState } from 'react';
import { TablePagination } from 'campfire-web';

export function Default() {
  const [pageIndex, setPageIndex] = useState(2);
  const [pageSize, setPageSize] = useState(25);
  return (
    <div className="border border-border rounded-lg bg-card" style={{ maxWidth: 760 }}>
      <TablePagination
        pageIndex={pageIndex}
        pageSize={pageSize}
        totalRows={1284}
        onPageChange={setPageIndex}
        onPageSizeChange={setPageSize}
      />
    </div>
  );
}

export function FirstPage() {
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  return (
    <div className="border border-border rounded-lg bg-card" style={{ maxWidth: 760 }}>
      <TablePagination
        pageIndex={pageIndex}
        pageSize={pageSize}
        totalRows={312}
        onPageChange={setPageIndex}
        onPageSizeChange={setPageSize}
      />
    </div>
  );
}
