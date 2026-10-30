# India Post Integration - Test Report

| | |
|---|---|
| Application | Yarn Guy storefront (Django) |
| Application version | commit `5e34659` |
| Generated | 2026-10-23T08:44:18+00:00 |
| India Post staging URL | `https://test.cept.gov.in/beextcustomer` |
| Environment | Python 3.11.9, Django 5.1.15 |
| Automated test suites | indiapost, returns, orders.test_carrier_independence |
| **Automated module tests** | **ALL PASSED** - 180 of 180 passed, 0 failed, 0 errors, 0 skipped (227.1 s) |
| **Staging (UAT) scenarios** | **NOT EXECUTED** - Cannot reach the staging server (TLS): test.cept.gov.in accepted the connection and then dropped the TLS handshake: [WinError 10054] An existing connection was forcibly closed by the remote host |

## 1. Automated module tests

These run the integration code against a *simulated* India Post server, so every success, error, token-expiry, duplicate and outage path can be exercised. They are not a substitute for the staging scenarios in section 2.

### Summary by module

| Module / area | Tests | Passed | Failed | Errors | Skipped |
|---|---:|---:|---:|---:|---:|
| API client: authentication, token refresh, error handling | 6 | 6 | 0 | 0 | 0 |
| Admin and dashboard actions | 8 | 8 | 0 | 0 | 0 |
| Booking, parcel rules and labels | 26 | 26 | 0 | 0 | 0 |
| Checkout address validation | 12 | 12 | 0 | 0 | 0 |
| Configuration and barcode pool | 7 | 7 | 0 | 0 | 0 |
| Customer returns: approval, pickup and close-out | 39 | 39 | 0 | 0 | 0 |
| Customer returns: eligibility and requests | 33 | 33 | 0 | 0 | 0 |
| Independence from Delhivery | 11 | 11 | 0 | 0 | 0 |
| Retries and background tasks | 3 | 3 | 0 | 0 | 0 |
| Reverse pickup (India Post PICKUP booking) | 6 | 6 | 0 | 0 | 0 |
| Staging runner and connectivity diagnosis | 15 | 15 | 0 | 0 | 0 |
| Tracking: webhook and periodic sync | 14 | 14 | 0 | 0 | 0 |
| **Total** | **180** | **180** | **0** | **0** | **0** |

### Every test case

#### API client: authentication, token refresh, error handling

| # | Test case | Result | Time (s) |
|---:|---|---|---:|
| 1 | error classes | **PASS** | 0.31 |
| 2 | expired token is refreshed and the call retried once | **PASS** | 0.31 |
| 3 | invalid credentials raise an auth error | **PASS** | 0.31 |
| 4 | login token is cached and scoped to host and user | **PASS** | 0.31 |
| 5 | network error is transient | **PASS** | 0.30 |
| 6 | recorder masks credentials and tokens | **PASS** | 0.31 |

#### Admin and dashboard actions

| # | Test case | Result | Time (s) |
|---:|---|---|---:|
| 1 | already booked orders are not resubmitted | **PASS** | 0.69 |
| 2 | failures are reported with the reason | **PASS** | 0.69 |
| 3 | clearing a manual barcode makes the order bookable again | **PASS** | 0.70 |
| 4 | duplicate manual barcode is a friendly error | **PASS** | 0.62 |
| 5 | manual barcode is never auto booked | **PASS** | 0.62 |
| 6 | a delhivery order shows only the delhivery section | **PASS** | 0.38 |
| 7 | an india post order shows only the india post section | **PASS** | 0.36 |
| 8 | the settings page offers both couriers | **PASS** | 0.36 |

#### Booking, parcel rules and labels

| # | Test case | Result | Time (s) |
|---:|---|---|---:|
| 1 | barcode check digit is valid s10 | **PASS** | 0.33 |
| 2 | cancelled and refunded orders cannot be booked | **PASS** | 0.31 |
| 3 | cod booking sends cod flag and order total | **PASS** | 0.33 |
| 4 | label failure never undoes a booking | **PASS** | 0.31 |
| 5 | payload uses real parcel data and clean article type | **PASS** | 0.31 |
| 6 | prepaid booking stores barcode label and tariff | **PASS** | 0.31 |
| 7 | rejected booking is recorded and retryable | **PASS** | 0.31 |
| 8 | repeat booking is idempotent | **PASS** | 0.33 |
| 9 | server error is retryable client error is not | **PASS** | 0.31 |
| 10 | success response without a summary is accepted | **PASS** | 0.31 |
| 11 | success with article errors is not a booking | **PASS** | 0.31 |
| 12 | guest order without any name uses a generic one | **PASS** | 0.33 |
| 13 | missing snapshot name falls back to the account name | **PASS** | 0.31 |
| 14 | failed attempts reuse one barcode | **PASS** | 0.33 |
| 15 | validation failures do not consume barcodes | **PASS** | 0.33 |
| 16 | a booking in progress blocks a second one | **PASS** | 0.33 |
| 17 | a crash never leaves the claim dangling | **PASS** | 0.34 |
| 18 | a second request arriving mid booking sends nothing | **PASS** | 0.34 |
| 19 | a stale claim from a crashed worker can be taken over | **PASS** | 0.34 |
| 20 | label always uses contract payment and payment completed | **PASS** | 0.34 |
| 21 | heavy article is a parcel with parcel minimums | **PASS** | 0.33 |
| 22 | light but thick article is a parcel | **PASS** | 0.31 |
| 23 | light flat article is a document | **PASS** | 0.31 |
| 24 | over limits are refused | **PASS** | 0.33 |
| 25 | quantity multiplies weight and missing weight defaults to 500g | **PASS** | 0.36 |
| 26 | short names are padded to the three character minimum | **PASS** | 0.36 |

#### Checkout address validation

| # | Test case | Result | Time (s) |
|---:|---|---|---:|
| 1 | booking uses the same mobile rules | **PASS** | 0.33 |
| 2 | checkout accepts mobiles starting with 91 | **PASS** | 0.47 |
| 3 | checkout rejects invalid mobile and pincode | **PASS** | 0.42 |
| 4 | normalize indian mobile | **PASS** | 0.30 |
| 5 | normalize pincode | **PASS** | 0.34 |
| 6 | address model and forms reject bad pincodes | **PASS** | 0.31 |
| 7 | checkout rejects a saved address with an invalid pin | **PASS** | 0.36 |
| 8 | valid values still pass | **PASS** | 0.31 |
| 9 | india post mode shows a required post office field | **PASS** | 0.36 |
| 10 | other carriers keep the optional address line 2 | **PASS** | 0.34 |
| 11 | post office is required server side only in india post mode | **PASS** | 0.42 |
| 12 | post office travels to india post as address line 2 | **PASS** | 0.31 |

#### Configuration and barcode pool

| # | Test case | Result | Time (s) |
|---:|---|---|---:|
| 1 | blank base url fails loudly instead of defaulting to test | **PASS** | 0.31 |
| 2 | exhausted pool is recorded on the shipment | **PASS** | 0.31 |
| 3 | no pool means a clear error never an implicit pool | **PASS** | 0.31 |
| 4 | seed command creates the uat pool explicitly | **PASS** | 0.33 |
| 5 | complete configuration has no problems | **PASS** | 0.33 |
| 6 | each missing setting is reported before any request | **PASS** | 0.36 |
| 7 | label with missing config does not crash | **PASS** | 0.31 |

#### Customer returns: approval, pickup and close-out

| # | Test case | Result | Time (s) |
|---:|---|---|---:|
| 1 | an approved request can still be rejected before pickup | **PASS** | 0.50 |
| 2 | approval email depends on the courier | **PASS** | 0.39 |
| 3 | approve twice is refused | **PASS** | 0.34 |
| 4 | approving only agrees it nothing is booked | **PASS** | 0.33 |
| 5 | rejection needs a reason and emails the customer | **PASS** | 0.34 |
| 6 | damaged lines are not restocked | **PASS** | 0.33 |
| 7 | delhivery orders go straight from approved to received | **PASS** | 0.34 |
| 8 | full india post lifecycle | **PASS** | 0.36 |
| 9 | refund amount can be overridden and cannot be negative | **PASS** | 0.34 |
| 10 | refund and replacement cannot be mixed up | **PASS** | 0.36 |
| 11 | refund path restocks and records the amount | **PASS** | 0.33 |
| 12 | replacement is all or nothing when stock is short | **PASS** | 0.34 |
| 13 | replacement path takes the items out of stock again | **PASS** | 0.34 |
| 14 | returns never change the order status | **PASS** | 0.34 |
| 15 | steps cannot be skipped or repeated | **PASS** | 0.33 |
| 16 | variant stock is used when the line has a variant | **PASS** | 0.33 |
| 17 | a requested return shows approve and reject but no pickup | **PASS** | 0.67 |
| 18 | approving then initiating the pickup for an india post order | **PASS** | 0.70 |
| 19 | bad input and unknown actions | **PASS** | 0.67 |
| 20 | booking failure is shown and can be retried | **PASS** | 0.67 |
| 21 | delhivery order shows no initiate pickup option | **PASS** | 0.69 |
| 22 | full refund flow through the dashboard | **PASS** | 0.75 |
| 23 | full replacement flow through the dashboard | **PASS** | 0.72 |
| 24 | list filter and search | **PASS** | 0.69 |
| 25 | new request appears in the returns section with a pending badge | **PASS** | 0.70 |
| 26 | refund form for refund requests and replacement form for replacements | **PASS** | 0.69 |
| 27 | rejecting from the dashboard | **PASS** | 0.73 |
| 28 | staff only | **PASS** | 0.70 |
| 29 | stock shortfall on replacement is reported | **PASS** | 0.69 |
| 30 | the dashboard order page lists its returns | **PASS** | 0.77 |
| 31 | delhivery orders have no pickup option | **PASS** | 0.36 |
| 32 | failed booking keeps the approval and reuses the barcode on retry | **PASS** | 0.39 |
| 33 | initiating books an india post pickup with a reserved barcode | **PASS** | 0.38 |
| 34 | initiating twice sends one booking | **PASS** | 0.38 |
| 35 | manual tracking number is the fallback | **PASS** | 0.42 |
| 36 | missing address data is a recorded error not a crash | **PASS** | 0.38 |
| 37 | only the returned items weigh into the parcel | **PASS** | 0.42 |
| 38 | pickup date and slot are validated | **PASS** | 0.39 |
| 39 | pickup requires an approved return | **PASS** | 0.41 |

#### Customer returns: eligibility and requests

| # | Test case | Result | Time (s) |
|---:|---|---|---:|
| 1 | another customers order is refused | **PASS** | 0.70 |
| 2 | creates a request per product with its own quantity and reason | **PASS** | 0.33 |
| 3 | customer and store are emailed | **PASS** | 0.34 |
| 4 | customer can cancel only before approval | **PASS** | 0.34 |
| 5 | invalid resolution is refused | **PASS** | 0.31 |
| 6 | outside the window is refused server side | **PASS** | 0.34 |
| 7 | quantity cannot exceed what was bought or what is left | **PASS** | 0.34 |
| 8 | resolution defaults to refund and replacement is recorded | **PASS** | 0.33 |
| 9 | second request gets the next number | **PASS** | 0.33 |
| 10 | the delivering courier is recorded on the request | **PASS** | 0.34 |
| 11 | validation errors | **PASS** | 0.33 |
| 12 | a customer cannot see or return someone elses order | **PASS** | 0.67 |
| 13 | a reserved barcode is not shown before the pickup is booked | **PASS** | 0.38 |
| 14 | closed window shows the message instead of the form | **PASS** | 0.33 |
| 15 | detail shows pickup tracking only once booked | **PASS** | 0.36 |
| 16 | detail wording follows the stage and courier | **PASS** | 0.36 |
| 17 | form errors are shown and nothing is created | **PASS** | 0.34 |
| 18 | form offers refund or replacement | **PASS** | 0.36 |
| 19 | list and detail render | **PASS** | 0.36 |
| 20 | login is required | **PASS** | 0.36 |
| 21 | order page offers return buttons only while eligible | **PASS** | 0.39 |
| 22 | submitting the form creates the request with the chosen resolution | **PASS** | 0.36 |
| 23 | delivered order inside the window is eligible | **PASS** | 0.33 |
| 24 | fully returned order is no longer eligible | **PASS** | 0.34 |
| 25 | last day of the window is inclusive and the next day is closed | **PASS** | 0.36 |
| 26 | quantities already in open requests are not returnable again | **PASS** | 0.36 |
| 27 | rejected and cancelled requests release their quantity | **PASS** | 0.34 |
| 28 | undelivered orders are not eligible | **PASS** | 0.34 |
| 29 | window comes from site settings | **PASS** | 0.34 |
| 30 | zero turns returns off | **PASS** | 0.33 |
| 31 | blank keeps the saved window and zero is allowed | **PASS** | 0.55 |
| 32 | unreasonable values are rejected | **PASS** | 0.38 |
| 33 | window can be changed and applies immediately | **PASS** | 0.38 |

#### Independence from Delhivery

| # | Test case | Result | Time (s) |
|---:|---|---|---:|
| 1 | books and tracks with india post completely unconfigured | **PASS** | 0.03 |
| 2 | the india post sync job is harmless when india post is unconfigured | **PASS** | 0.00 |
| 3 | books and tracks with delhivery completely unconfigured | **PASS** | 0.03 |
| 4 | the delhivery webhook is closed when delhivery is unconfigured | **PASS** | 0.00 |
| 5 | a courier is never booked on top of the other | **PASS** | 0.02 |
| 6 | dispatch follows the locked courier not the current setting | **PASS** | 0.02 |
| 7 | each order is locked to the courier active when it was confirmed | **PASS** | 0.03 |
| 8 | a delhivery scan only moves the delhivery order | **PASS** | 0.02 |
| 9 | an india post scan only moves the india post order | **PASS** | 0.03 |
| 10 | each webhook ignores the other couriers identifiers | **PASS** | 0.02 |
| 11 | the india post sync never polls delhivery shipments | **PASS** | 0.00 |

#### Retries and background tasks

| # | Test case | Result | Time (s) |
|---:|---|---|---:|
| 1 | confirmed order routes to the retrying task | **PASS** | 0.36 |
| 2 | permanent failure is recorded and not retried | **PASS** | 0.31 |
| 3 | transient failure schedules a retry with backoff | **PASS** | 0.31 |

#### Reverse pickup (India Post PICKUP booking)

| # | Test case | Result | Time (s) |
|---:|---|---|---:|
| 1 | afternoon slot uses 1 pm | **PASS** | 0.34 |
| 2 | books a pickup from the customer to the store | **PASS** | 0.33 |
| 3 | invalid slot is refused before any request | **PASS** | 0.31 |
| 4 | no office for the pickup pin is a clear error | **PASS** | 0.31 |
| 5 | pickup fields follow the documented formats | **PASS** | 0.34 |
| 6 | rejection and outage are classified | **PASS** | 0.33 |

#### Staging runner and connectivity diagnosis

| # | Test case | Result | Time (s) |
|---:|---|---|---:|
| 1 | blocked port | **PASS** | 0.31 |
| 2 | command stops with the diagnosis instead of running every scenario | **PASS** | 0.31 |
| 3 | dns failure | **PASS** | 0.33 |
| 4 | empty url | **PASS** | 0.31 |
| 5 | reachable server | **PASS** | 0.30 |
| 6 | the check can be skipped | **PASS** | 0.42 |
| 7 | tls reset is diagnosed as an ip allowlist with the public ip | **PASS** | 0.31 |
| 8 | a broken server is reported as failures not hidden | **PASS** | 0.42 |
| 9 | all scenarios pass against a healthy server | **PASS** | 0.42 |
| 10 | command refuses a non uat host without the flag | **PASS** | 0.34 |
| 11 | command runs end to end and exits cleanly | **PASS** | 0.56 |
| 12 | evidence and report are written and secrets are masked | **PASS** | 0.55 |
| 13 | failure scenarios send no booking request for local errors | **PASS** | 0.41 |
| 14 | token refresh scenario records the 401 then relogin sequence | **PASS** | 0.44 |
| 15 | uat orders are cancelled afterwards so they never count as sales | **PASS** | 0.45 |

#### Tracking: webhook and periodic sync

| # | Test case | Result | Time (s) |
|---:|---|---|---:|
| 1 | duplicate and out of order events are ignored | **PASS** | 26.48 |
| 2 | fails closed when no token is configured | **PASS** | 0.31 |
| 3 | header and bearer tokens update the order | **PASS** | 51.75 |
| 4 | rejects missing or wrong token | **PASS** | 0.31 |
| 5 | return scan cannot cancel a delivered order | **PASS** | 25.94 |
| 6 | token in query string is not accepted | **PASS** | 0.34 |
| 7 | a delivery event closes the shipment for future syncs | **PASS** | 27.42 |
| 8 | closed orders are not polled and open ones are | **PASS** | 0.33 |
| 9 | least recently synced shipments go first and batches are split | **PASS** | 0.33 |
| 10 | unbooked shipments are never polled | **PASS** | 0.33 |
| 11 | laevent is chosen by date not position | **PASS** | 0.31 |
| 12 | parses the documented response shape | **PASS** | 0.33 |
| 13 | rts flag returns the order to the store | **PASS** | 5.36 |
| 14 | sync moves orders from real event names | **PASS** | 25.83 |

## 2. Staging (UAT) scenarios against the real India Post server

**Not executed.** Cannot reach the staging server (TLS): test.cept.gov.in accepted the connection and then dropped the TLS handshake: [WinError 10054] An existing connection was forcibly closed by the remote host

India Post's servers only accept whitelisted IP addresses. Ask integrations.cept@indiapost.gov.in to whitelist this machine's public IP (223.188.96.120), then run the command again.

These scenarios are required evidence for production activation. Fix the cause above and run the command again; nothing in this section is simulated or assumed.
