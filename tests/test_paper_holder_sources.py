import unittest
from sniper.krypton.checks import holder_facts,TOKEN_2022
from sniper.krypton.demo import address

class Token2022EvidenceTests(unittest.TestCase):
    def facts(self,extension='tokenMetadata'):
        mint=address(1)
        account={'owner':TOKEN_2022,'executable':False,'data':{'parsed':{'type':'mint','info':{
            'supply':'100','decimals':0,'mintAuthority':None,'freezeAuthority':None,'extensions':[{'extension':extension}]}}}}
        holding={'owner':TOKEN_2022,'executable':False,'data':{'parsed':{'type':'account','info':{
            'mint':mint,'owner':address(2),'tokenAmount':{'amount':'100','decimals':0},'extensions':[{'extension':'immutableOwner'}]}}}}
        return holder_facts(mint,account,[(address(3),holding)],complete=True)

    def test_metadata_only_keeps_actual_concentration(self):
        facts=self.facts()
        self.assertTrue(facts['complete_owner_coverage'])
        self.assertEqual(facts['max_single_holder'],'1')
        self.assertTrue(facts['mint_disabled'])

    def test_transfer_economics_and_unknown_extensions_never_pass(self):
        for extension in ('transferFeeConfig','transferHook','defaultAccountState','permanentDelegate','confidentialTransferMint','unknown'):
            with self.subTest(extension=extension),self.assertRaises(ValueError):self.facts(extension)
