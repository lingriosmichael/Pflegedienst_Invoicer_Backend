import os
import json
import app.pdf_parser
import app.database
import app.invoice_generator

if __name__ == "__main__":
    app.database.init_db()
    abrechnungsmonat = input("\U0001F4C9  Abrechnungsmonat (e.g. April 2025): ").strip()

    while True:
        print("\n\U0001F4CC Select process to execute:")
        print("1. Import and add to database")
        print("2. Generate invoices")
        print("3. Complete missing patient data")
        print("4. Check service fields")
        print("5. Retry failed chunks")
        print("6. Regenerate an invoice")
        print("7. Exit")
        choice = input("Enter 1, 2, 3, 4 or 5: ").strip()

        if choice == "1":
            text = app.pdf_parser.extract_text_from_pdf("data/abrechnung/june_2025_2.pdf")
            chunks = app.pdf_parser.split_into_chunks(text)
            print(f"\U0001F50D Found {len(chunks)} chunks in PDF.")
            sgbxi_entlastungsleistung = input("\U0001F4C9 sgbxi or entleistung: ").strip()            
            filtered_chunks = app.pdf_parser.filter_chunks_by_mode(chunks, sgbxi_entlastungsleistung)
            app.pdf_parser.process_import_sgbxi(filtered_chunks, abrechnungsmonat)

        elif choice == "2":
            app.invoice_generator.process_generate_invoices(abrechnungsmonat)

        elif choice == "3":
            app.database.check_missing_patient_fields(abrechnungsmonat)
            app.database.check_service_fields()
        
        elif choice == "4":
            app.database.check_service_fields()

        elif choice == "5":
            app.pdf_parser.refeed_failed_chunk_from_file()

        elif choice == "6":
            invoice_num = input("Invoice number to regenerate: ").strip()
            app.invoice_generator.regenerate_invoice(invoice_num)

        elif choice == "7":
            print("👋 Exiting.")
            break

        else:
            print("❌ Invalid choice. Try again.")
