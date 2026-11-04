INSERT INTO flights
(id, flight_number, airline, origin, destination, departure_at, arrival_at,
 total_seats, available_seats, price_minor, status)
VALUES
('11111111-1111-4111-8111-111111111111', 'SU1234', 'Аэрофлот', 'SVO', 'LED',
 '2026-12-01T09:00:00Z', '2026-12-01T10:30:00Z', 180, 180, 550000, 'SCHEDULED'),
('22222222-2222-4222-8222-222222222222', 'DP201', 'Победа', 'VKO', 'LED',
 '2026-12-01T12:00:00Z', '2026-12-01T13:30:00Z', 189, 189, 390000, 'SCHEDULED'),
('33333333-3333-4333-8333-333333333333', 'SU1236', 'Аэрофлот', 'SVO', 'LED',
 '2026-12-02T15:00:00Z', '2026-12-02T16:30:00Z', 180, 180, 620000, 'SCHEDULED');
