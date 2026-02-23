from __future__ import annotations

import customtkinter as ctk


class CreditsWindow(ctk.CTkToplevel):
    def __init__(self, parent: ctk.CTk) -> None:
        super().__init__(parent)
        self.title("DustyBot - Credits")
        self.geometry("760x560")
        self.minsize(680, 500)
        self.transient(parent)

        self._contact_name = "J.S. Jassar"
        self._contact_email = "jasrajjassar775@gmail.com"

        self._build_ui()

    def _build_ui(self) -> None:
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=20)

        title = ctk.CTkLabel(container, text="DustyBot Credits", font=ctk.CTkFont(size=26, weight="bold"))
        title.pack(anchor="w")

        subtitle = ctk.CTkLabel(
            container,
            text="Contact details and Terms & Conditions",
            text_color="gray70",
        )
        subtitle.pack(anchor="w", pady=(4, 14))

        contact_card = ctk.CTkFrame(container, corner_radius=12)
        contact_card.pack(fill="x")

        contact_title = ctk.CTkLabel(contact_card, text="Contact", font=ctk.CTkFont(size=18, weight="bold"))
        contact_title.pack(anchor="w", padx=14, pady=(12, 6))

        contact_rows = [
            f"Name: {self._contact_name}",
            f"Email: {self._contact_email}",
        ]
        for row in contact_rows:
            ctk.CTkLabel(contact_card, text=row, anchor="w", justify="left").pack(fill="x", padx=14, pady=2)

        terms_title = ctk.CTkLabel(container, text="Terms & Conditions (T&C)", font=ctk.CTkFont(size=18, weight="bold"))
        terms_title.pack(anchor="w", pady=(16, 8))

        terms_box = ctk.CTkTextbox(container, wrap="word", height=260)
        terms_box.pack(fill="both", expand=True)
        terms_box.insert(
            "1.0",
            "\n".join(
                [
                    "1. DustyBot is intended for internal production workflow support only.",
                    "2. Operators must review all generated outputs before release or print.",
                    "3. Engineering approval remains required where applicable.",
                    "4. DustyBot does not guarantee completeness or fitness for a specific use case.",
                    "5. Use of this software is at your own operational risk.",
                    "6. Confidential files must stay within approved company systems.",
                    "7. By using DustyBot, you accept these Terms & Conditions.",
                ]
            ),
        )
        terms_box.configure(state="disabled")

        button_row = ctk.CTkFrame(container, fg_color="transparent")
        button_row.pack(fill="x", pady=(12, 0))
        ctk.CTkButton(button_row, text="Close", width=120, command=self.destroy).pack(side="right")
