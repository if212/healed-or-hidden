# Bug-script neutralization report

Written by `python -m hoh.neutralize --report` (`just neutralize`). Do not edit by hand:
a test compares this file with a fresh build.

Every selected bug script is rewritten before it is used (design doc section 5.3). The
rewrite does four things. None of them changes what a bug adds, removes or changes on
the page.

1. It drops comments. The four `BEGIN`/`END` marker lines stay, because the benchmark's
   loader needs them; they never reach the page.
2. It replaces each `console.*` call with `void 0`.
3. It applies the literal rules below, which live in `scenarios/neutralize/<app>.yaml`.
4. It renames the injector's global names and the key passed to `transition()`.

The rewritten scripts are written to `build/payloads/` and are not committed.

- Scripts: 54
- Literal rules: 47, in 20 scripts
- Console calls replaced: 46
- Scripts that call the injector and had its name and key renamed: 17

## Literal rules

One line per rule. `xN` after a value means it is replaced in N places.

| App | Task | Find | Replace | Why |
|---|---|---|---|---|
| bookstack | count_recently_created_books | `custom-id` | `4` | Entity ids are numbers. The seed has books 1 to 3, so an extra book is 4. |
| bookstack | count_recently_created_books | `newBook.href = '#'` | `newBook.href = window.location.origin + '/books/book3'` | No real list entry links to "#". A real book links to /books/ plus its slug, on the app's own origin. |
| bookstack | count_recently_created_books | `Custom Book Title` | `Book3` | Invented title. The seeded books are Book, Book1 and Book2. |
| bookstack | count_recently_created_books | `Custom Book Description` | `Description` | Invented text. Every seeded book has the description "Description". |
| bookstack | count_recently_created_chapters | `custom-id` | `5` | Entity ids are numbers. The seed has chapters 1 to 4, so an extra chapter is 5. |
| bookstack | count_recently_created_chapters | `newChapter.href = '#'` | `newChapter.href = window.location.origin + '/books/book2/chapter/chapter-3'` | No real list entry links to "#". A real chapter links to its book and its own slug. |
| bookstack | count_recently_created_chapters | `Custom Chapter Title` | `Chapter 3` | Invented title. Book2 already holds Chapter 1 and Chapter 2. |
| bookstack | count_recently_created_chapters | `Custom Chapter Description` | `Description` | Invented text. Every seeded chapter has the description "Description". |
| bookstack | count_recently_created_pages | `fakeEntry.href = "#"` | `fakeEntry.href = window.location.origin + "/books/book1/page/page-3"` | No real search result links to "#". A real page links to its book and its own slug. |
| bookstack | count_recently_created_pages | `"999"` | `"7"` | Made-up id. The seed has pages 1 to 6, so an extra page is 7. |
| bookstack | count_recently_created_pages | `Some Page` | `Page 3` | Placeholder title. Book1 already holds Page 1, Page 2 and Page. |
| bookstack | count_recently_created_pages | `Some Book` | `Book1` | Placeholder book name. Book1 is a seeded book. |
| bookstack | count_recently_created_pages | `This is a page description.` | `Page Description` | Placeholder text. Seeded pages read "Page Description". |
| bookstack | count_recently_created_shelves | `custom-id` | `2` | Entity ids are numbers. The seed has one shelf, so an extra shelf is 2. |
| bookstack | count_recently_created_shelves | `newShelf.href = '#'` | `newShelf.href = window.location.origin + '/shelves/shelf-2'` | No real list entry links to "#". A real shelf links to /shelves/ plus its slug. |
| bookstack | create_book | `Bad Description` | `Book2 Description` | The word "Bad" labels the value as wrong. The new value is still not what the test typed. |
| bookstack | delete_book | `'999'` | `'3'` | Made-up id. The copied card is the seeded book "Book", whose id is 3. |
| bookstack | delete_book | `'/books/book-duplicate'` | `window.location.origin + '/books/book'` | The link named the card a duplicate. A second card for "Book" links to the same book. |
| invoiceninja | approve_quote | `/quotes/${number}/edit` | `/quotes/Wpmbk5ezJn/edit` | The row is the seeded quote 123456 (row 2). Its real link carries the id code, not the quote number. |
| invoiceninja | approve_quote | `/clients/${client}` | `/clients/VolejRejNm` | The client company_name is row 1. Its real link carries the id code, not the name. |
| invoiceninja | delete_expired_quote | `/quotes/${number}/edit` | `/quotes/VolejRejNm/edit` | The row is the seeded quote 123456_expired (row 1). Its real link carries the id code, not the quote number. |
| invoiceninja | delete_expired_quote | `/clients/${client}` | `/clients/VolejRejNm` | The client company_name is row 1. Its real link carries the id code, not the name. |
| invoiceninja | delete_payment | `/payments/NewInvoice/edit` | `/payments/VolejRejNm/edit` | Placeholder id. The row is the seeded payment 0001 (row 1). |
| invoiceninja | delete_payment | `/clients/NewCompany` | `/clients/VolejRejNm` | Placeholder id. The client company_name is row 1. |
| invoiceninja | delete_payment | `/invoices/NewInvoice/edit` | `/invoices/VolejRejNm/edit` | Placeholder id. The payment was applied to the seeded invoice 123456 (row 1). |
| invoiceninja | expired_quotes | `NewQuote001` | `wMvbmOeYAl` | Placeholder id. The seed has three quotes, so an extra quote is row 4. |
| invoiceninja | expired_quotes | `/clients/${quoteId}` | `/clients/Wpmbk5ezJn` | The client link reused the quote's id. A second client, company_name_2, is row 2. |
| invoiceninja | expired_quotes | `" $25,000.00"` | `"$ 25,000.00"` | The app writes amounts with a space after the sign, like every other row of the table. |
| invoiceninja | mark_invoice_as_paid | `/invoices/FakeInvoice/edit` | `/invoices/Wpmbk5ezJn/edit` | The id said "Fake". The row is the seeded invoice 123456_sent (row 2). |
| invoiceninja | mark_invoice_as_paid | `/clients/FakeClient` | `/clients/VolejRejNm` | The id said "Fake". The client company_name is row 1. |
| invoiceninja | mark_past_due_invoice_as_paid | `/invoices/FakeInvoice/edit` | `/invoices/wMvbmOeYAl/edit` | The id said "Fake". The row is the seeded invoice 123456_past_due (row 4). |
| invoiceninja | mark_past_due_invoice_as_paid | `/clients/FakeClient` | `/clients/VolejRejNm` | The id said "Fake". The client company_name is row 1. |
| invoiceninja | past_due_invoices | `FakeInvoice123` | `4openRe7Az` | The id said "Fake". The seed has four invoices, so an extra invoice is row 5. |
| invoiceninja | past_due_invoices | `/clients/FakeClient` | `/clients/Wpmbk5ezJn` | The id said "Fake". A second client, company_name_2, is row 2. |
| invoiceninja | recent_payments | `FakeInvoice123` | `Wpmbk5ezJn` | The id said "Fake". The seed has one payment, so an extra payment is row 2. |
| invoiceninja | recent_payments | `/clients/FakeClient` | `/clients/Wpmbk5ezJn` | The id said "Fake". A second client, company_name_2, is row 2. |
| invoiceninja | recent_payments | `/invoices/${invoiceId}/edit` | `/payments/${invoiceId}/edit` | In the payments table the number links to the payment, not to an invoice. |
| invoiceninja | upcoming_invoices | `FakeInvoice123` | `4openRe7Az` | The id said "Fake". The seed has four invoices, so an extra invoice is row 5. |
| invoiceninja | upcoming_invoices | `/clients/FakeClient` | `/clients/Wpmbk5ezJn` | The id said "Fake". A second client, company_name_2, is row 2. |
| invoiceninja | upcoming_quotes | `NewQuote001` | `wMvbmOeYAl` | Placeholder id. The seed has three quotes, so an extra quote is row 4. |
| invoiceninja | upcoming_quotes | `/clients/${quoteId}` | `/clients/Wpmbk5ezJn` | The client link reused the quote's id. A second client, company_name_2, is row 2. |
| invoiceninja | upcoming_recurring_invoices | `NewInvoiceId` x2 | `Wpmbk5ezJn` | Placeholder id, used for the checkbox and for the link. The seed has one recurring invoice, so an extra one is row 2. |
| invoiceninja | upcoming_recurring_invoices | `/clients/NewCompany` | `/clients/Wpmbk5ezJn` | Placeholder id. A second client, company_name_2, is row 2. |
| invoiceninja | view_invoice_details | `'$180,000.00'` | `'$ 180,000.00'` | The app writes amounts with a space after the sign. Only the amount should be wrong, not its format. |
| invoiceninja | view_quote_details | `'$730,000.00'` | `'$ 730,000.00'` | The app writes amounts with a space after the sign. Only the amount should be wrong, not its format. |
| indico | favourite_lecture | `eventTitleLink.style.color = "red";` | (removed) | Red title text only marks the changed row. The bug is the extra star, which stays. |
| indico | favourite_lecture | `eventTitleLink.style.fontWeight = "bold";` | (removed) | Bold title text only marks the changed row. The bug is the extra star, which stays. |

## Renamed names

The runtime's `bug_injector.js` and `init.py` use the same names. They must go through
`neutralize_identifiers` too when an environment is built, so that the three files agree.

| Name in the benchmark | Renamed to |
|---|---|
| `__BUG_INJECTOR_TRIGGERED__` | `__as_t` |
| `__BUG_INJECTOR_API__` | `__appState` |
| `__BUG_INJECTOR__` | `__as` |
| `__flash_ui_active__` | `__ui_ready` |
| `__BUG_INJECTOR_TRIGGERED_URL__` | `__as_u` |
| `__BUG_INJECTOR_TRANSITION_PREV__` | `__as_p` |
| `__BUG_INJECTOR_TRANSITION_COUNT__` | `__as_n` |
| first argument of `transition()`, which is `<app>_<task>` | `st` |

## Changes per script

| App | Task | Console calls replaced | Literal rules | Injector names renamed | Transition keys renamed |
|---|---|---|---|---|---|
| bookstack | comment | 1 | 0 | 0 | 0 |
| bookstack | count_recently_created_books | 1 | 4 | 0 | 0 |
| bookstack | count_recently_created_chapters | 1 | 4 | 0 | 0 |
| bookstack | count_recently_created_pages | 0 | 5 | 0 | 0 |
| bookstack | count_recently_created_shelves | 1 | 2 | 0 | 0 |
| bookstack | create_book | 0 | 1 | 1 | 1 |
| bookstack | create_page | 0 | 0 | 0 | 0 |
| bookstack | create_sort_rule | 0 | 0 | 1 | 1 |
| bookstack | delete_book | 0 | 2 | 1 | 1 |
| bookstack | favourite_book | 2 | 0 | 0 | 0 |
| bookstack | recent_activity_all | 0 | 0 | 0 | 0 |
| bookstack | recent_activity_chapter | 1 | 0 | 0 | 0 |
| bookstack | recent_activity_page | 0 | 0 | 0 | 0 |
| bookstack | recent_activity_shelf | 1 | 0 | 0 | 0 |
| bookstack | recently_viewed_book | 0 | 0 | 1 | 1 |
| bookstack | recently_viewed_chapter | 0 | 0 | 1 | 1 |
| bookstack | search | 0 | 0 | 0 | 0 |
| bookstack | settings | 0 | 0 | 0 | 0 |
| bookstack | unfavourite_shelf | 0 | 0 | 0 | 0 |
| bookstack | update_book | 0 | 0 | 1 | 1 |
| invoiceninja | approve_quote | 1 | 2 | 1 | 1 |
| invoiceninja | convert_quote_to_invoice | 1 | 0 | 0 | 0 |
| invoiceninja | create_invoice | 1 | 0 | 0 | 0 |
| invoiceninja | create_product | 0 | 0 | 0 | 0 |
| invoiceninja | delete_expired_quote | 1 | 2 | 1 | 1 |
| invoiceninja | delete_payment | 0 | 3 | 1 | 1 |
| invoiceninja | disable_invoices_module | 0 | 0 | 1 | 1 |
| invoiceninja | disable_quotes_module | 0 | 0 | 1 | 1 |
| invoiceninja | enter_payment | 0 | 0 | 0 | 0 |
| invoiceninja | expired_quotes | 0 | 3 | 0 | 0 |
| invoiceninja | localization | 0 | 0 | 0 | 0 |
| invoiceninja | mark_invoice_as_paid | 0 | 2 | 1 | 1 |
| invoiceninja | mark_past_due_invoice_as_paid | 0 | 2 | 1 | 1 |
| invoiceninja | past_due_invoices | 0 | 2 | 0 | 0 |
| invoiceninja | recent_payments | 0 | 3 | 0 | 0 |
| invoiceninja | recent_transactions_expenses | 2 | 0 | 1 | 1 |
| invoiceninja | settings | 0 | 0 | 0 | 0 |
| invoiceninja | stop_recurring_invoice | 0 | 0 | 1 | 1 |
| invoiceninja | upcoming_invoices | 0 | 2 | 0 | 0 |
| invoiceninja | upcoming_quotes | 0 | 2 | 0 | 0 |
| invoiceninja | upcoming_recurring_invoices | 0 | 2 | 0 | 0 |
| invoiceninja | view_invoice_details | 1 | 1 | 0 | 0 |
| invoiceninja | view_products | 0 | 0 | 0 | 0 |
| invoiceninja | view_quote_details | 1 | 1 | 0 | 0 |
| indico | administration | 0 | 0 | 0 | 0 |
| indico | clone_event | 4 | 0 | 1 | 1 |
| indico | delete_conference | 3 | 0 | 0 | 0 |
| indico | favourite_lecture | 0 | 2 | 1 | 1 |
| indico | fill_out_survey | 0 | 0 | 0 | 0 |
| indico | profile_settings | 2 | 0 | 0 | 0 |
| indico | register_conference | 0 | 0 | 0 | 0 |
| indico | room_booking | 4 | 0 | 0 | 0 |
| indico | search_events | 3 | 0 | 0 | 0 |
| indico | view_conference_timetable | 14 | 0 | 0 | 0 |
