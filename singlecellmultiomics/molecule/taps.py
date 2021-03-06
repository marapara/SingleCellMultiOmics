import itertools
from singlecellmultiomics.molecule import Molecule
from singlecellmultiomics.molecule.nlaIII import NlaIIIMolecule
from singlecellmultiomics.molecule.nlaIII import AnnotatedNLAIIIMolecule
from singlecellmultiomics.molecule.chic import CHICMolecule
from singlecellmultiomics.molecule.chic import AnnotatedCHICMolecule
from uuid import uuid4
from collections import Counter
complement_trans = str.maketrans('ATGC', 'TACG')
from singlecellmultiomics.utils.sequtils import complement
from itertools import product

class TAPS():
    # Methylated Cs get converted into T readout
    def __init__(self, reference=None, reference_variants=None, **kwargs):
        """
        Intialise the TAPS class

        Args:
            reference (pysam.FastaFile) : reference fasta file

            reference_variants(pysam.VariantFile) : variants in comparison to supplied reference. @todo

        """
        assert reference is None, 'reference is now supplied by the molecule'
        if reference_variants is not None:
            raise NotImplementedError()

        self.overlap_tag = 'XM'

        """
        z unmethylated C in CpG context (CG)
        Z methylated C in CpG context (CG)
        x unmethylated C in CHG context ( C[ACT]G )
        X methylated C in CHG context   ( C[ACT]G )
        h unmethylated C in CHH context ( C[ACT][ACT] )
        H methylated C in CHH context ( C[ACT][ACT] )
        """
        self.context_mapping = {}
        self.context_mapping[False] = {
            'CGA': 'z',
            'CGT': 'z',
            'CGC': 'z',
            'CGG': 'z',
            'CAG': 'x',
            'CCG': 'x',
            'CTG': 'x',
        }

        self.context_mapping[True] = {
            context: letter.upper() for context,
            letter in self.context_mapping[False].items()}

        for x in list(itertools.product('ACT', repeat=2)):
            self.context_mapping[True][''.join(['C'] + list(x))] = 'H'
            self.context_mapping[False][''.join(['C'] + list(x))] = 'h'

    def position_to_context(
            self,
            chromosome,
            position,
            observed_base='N',
            strand=0,
            reference=None
        ):
        """Extract bismark call letter from a chromosomal location given the observed base

        Args:

            chromosome (str) : chromosome / contig of location to test

            position (int) : genomic location of location to test

            observed_base(str) : base observed in the read

            strand(str) : mapping strand

        Returns:
            context(str) : 3 basepair context
            bismark_letter(str) : bismark call
        """

        assert reference is not None

        qbase = observed_base.upper()

        ref_base = reference.fetch(
            chromosome, position, position + 1).upper()

        # if a vcf file is supplied  we can extract the possible reference bases
        # @todo

        context = None
        methylated = False
        rpos = position
        if ref_base == 'C' and strand == 0:
            context = reference.fetch(chromosome, rpos, rpos + 3).upper()

            if qbase == 'T':
                methylated = True
            methylationStateString = self.context_mapping[methylated].get(context, 'uU'[
                                                                          methylated])

        elif ref_base == 'G' and strand == 1:
            origin = reference.fetch(
                chromosome, rpos - 2, rpos + 1).upper()
            context = origin.translate(complement_trans)[::-1]
            if qbase == 'A':
                methylated = True

        return context, self.context_mapping[methylated].get(context, '.')

    def molecule_to_context_call_dict(self, molecule):
        """Extract bismark call_string dictionary from a molecule

        Args:
            molecule(singlecellmultiomics.molecule.Molecule) : molecule to extract calls from

        Returns:
            context_call_dict(dict) : {(chrom,pos):bismark call letter}
        """
        call_dict = {}  # (chrom,pos)-> "bismark" call_string
        for (chrom, pos), base in molecule.get_consensus().items():
            context, letter = self.position_to_context(chromosome=molecule.chromosome,
                                                       position=pos,
                                                       reference=molecule.reference,
                                                       observed_base=base,
                                                       strand=molecule.get_strand())
            if letter is not None:
                call_dict[(chrom, pos)] = letter
        return call_dict


class TAPSMolecule(Molecule):
    def __init__(self, fragments=None, taps=None, classifier=None, **kwargs):
        """ TAPSMolecule

        Args:
            fragments(list) :  list of fragments to associate with the Molecule
            taps(singlecellmultiomics.molecule.TAPS) : TAPS class which performs the methylation calling
            classifier : fitted sklearn classifier, when supplied this classifier is used to obtain a consensus from which the methylation calls are generated.

        """
        Molecule.__init__(self, fragments=fragments, **kwargs)
        if taps is None:
            raise ValueError("""Supply initialised TAPS class
                taps = singlecellmultiomics.molecule.TAPS( reference )
            """)
        self.taps = taps  # initialised TAPS class
        self.methylation_call_dict = None
        self.classifier = classifier

    def __finalise__(self):
        super().__finalise__()
        try:
            self.obtain_methylation_calls(classifier=self.classifier)
        except ValueError:
            pass

    def is_valid(self, set_rejection_reasons=False):
        if not super().is_valid(set_rejection_reasons=set_rejection_reasons):
            return False

        try:
            consensus = self.get_consensus(allow_unsafe=True)
        except ValueError:
            if set_rejection_reasons:
                self.set_rejection_reason('no_consensus')
            return False
        except TypeError:
            if set_rejection_reasons:
                self.set_rejection_reason('getPairGenomicLocations_failed')
            return False

        if self.methylation_call_dict is None:
            if set_rejection_reasons:
                self.set_rejection_reason('methylation_calls_failed')
            return False

        return True

    def obtain_methylation_calls(self, classifier=None):
        """ This methods returns a methylation call dictionary
            Args:
                classifier : classifier used for consensus determination
            returns:
                mcalls(dict) : (chrom,pos) : {'consensus': consensusBase, 'reference': referenceBase, 'call': call}
        """

        # Find all aligned positions and corresponding reference bases:
        aligned_reference_positions = {}  # (chrom,pos)->base
        for read in self.iter_reads():
            for read_pos, ref_pos, ref_base in read.get_aligned_pairs(
                    with_seq=True, matches_only=True):
                aligned_reference_positions[(
                    read.reference_name, ref_pos)] = ref_base.upper()

        # Obtain consensus:
        try:
            consensus = self.get_consensus(classifier=classifier, allow_unsafe=True)
        except ValueError:
            raise ValueError(
                'Cannot obtain a safe consensus for this molecule')

        # find all locations where a C/G was converted into A/T, now strand
        # specific
        converted_bases = 0
        conversions = {}
        for location, reference_base in aligned_reference_positions.items():
            if location not in consensus:
                continue
            if (not self.strand and reference_base == 'C' and consensus[location] in 'CT') or \
                    self.strand and reference_base == 'G' and consensus[location] in 'AG':
                conversions[location] = {
                    'ref': reference_base, 'obs': consensus[location]}
                if consensus[location] in 'TA':
                    converted_bases += 1

        # obtain the context of the conversions:
        conversion_contexts = {
            location:
            {'consensus': consensus[location],
             'reference_base': conversions[location]['ref'],
             'context': self.taps.position_to_context(
                *location,
                reference=self.reference,
                observed_base=observations['obs'],
                strand=self.strand)[1]}
            for location, observations in conversions.items()}

        # Write bismark tags:
        self.set_methylation_call_tags(conversion_contexts)


class TAPSNlaIIIMolecule(NlaIIIMolecule, TAPSMolecule):
    """Molecule class for combined TAPS and NLAIII """

    def __init__(self, fragments=None, taps=None, **kwargs):
        NlaIIIMolecule.__init__(self, fragments, **kwargs)
        TAPSMolecule.__init__(self, fragments=fragments, taps=taps, **kwargs)

    def write_tags(self):
        NlaIIIMolecule.write_tags(self)
        TAPSMolecule.write_tags(self)

    def __finalise__(self):
        NlaIIIMolecule.__finalise__(self)
        TAPSMolecule.__finalise__(self)

    def is_valid(self, set_rejection_reasons=False):
        return NlaIIIMolecule.is_valid(
            self, set_rejection_reasons=set_rejection_reasons) and TAPSMolecule.is_valid(
            self, set_rejection_reasons=set_rejection_reasons)


class AnnotatedTAPSNlaIIIMolecule(AnnotatedNLAIIIMolecule, TAPSMolecule):
    """Molecule class for combined TAPS, NLAIII and transcriptome """

    def __init__(self, fragments=None, features=None, taps=None, **kwargs):
        assert features is not None, "Supply features!"
        AnnotatedNLAIIIMolecule.__init__(
            self, fragments, features=features, **kwargs)
        TAPSMolecule.__init__(self, fragments=fragments, taps=taps, **kwargs)

    def write_tags(self):
        AnnotatedNLAIIIMolecule.write_tags(self)
        TAPSMolecule.write_tags(self)

    def is_valid(self, set_rejection_reasons=False):
        return AnnotatedNLAIIIMolecule.is_valid(
            self, set_rejection_reasons=set_rejection_reasons) and TAPSMolecule.is_valid(
            self, set_rejection_reasons=set_rejection_reasons)


class TAPSCHICMolecule(CHICMolecule, TAPSMolecule):
    """Molecule class for combined TAPS and CHIC """

    def __init__(self, fragments=None, taps=None, **kwargs):
        CHICMolecule.__init__(self, fragments, **kwargs)
        TAPSMolecule.__init__(self, fragments=fragments, taps=taps, **kwargs)

    def write_tags(self):
        CHICMolecule.write_tags(self)
        TAPSMolecule.write_tags(self)

    def is_valid(self, set_rejection_reasons=False):
        return CHICMolecule.is_valid(
            self, set_rejection_reasons=set_rejection_reasons) and TAPSMolecule.is_valid(
            self, set_rejection_reasons=set_rejection_reasons)

    def __finalise__(self):
        CHICMolecule.__finalise__(self)
        TAPSMolecule.__finalise__(self)


class AnnotatedTAPSCHICMolecule(AnnotatedCHICMolecule, TAPSMolecule):
    """Molecule class for combined TAPS, CHIC and transcriptome """

    def __init__(self, fragments=None, features=None, taps=None, **kwargs):
        assert features is not None, "Supply features!"
        AnnotatedCHICMolecule.__init__(
            self, fragments, features=features, **kwargs)
        TAPSMolecule.__init__(self, fragments=fragments, taps=taps, **kwargs)

    def write_tags(self):
        AnnotatedCHICMolecule.write_tags(self)
        TAPSMolecule.write_tags(self)

    def is_valid(self, set_rejection_reasons=False):
        return AnnotatedCHICMolecule.is_valid(
            self, set_rejection_reasons=set_rejection_reasons) and TAPSMolecule.is_valid(
            self, set_rejection_reasons=set_rejection_reasons)




def strip_array_tags(molecule):
    for read in molecule.iter_reads():
        read.set_tag('jM',None)
        read.set_tag('jI',None)
    return molecule



idmap = [''.join(p) for p in product('0123','0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ')]
context_ids = {''.join(ctx):idmap[i] for i,ctx in enumerate(product('ACTG',repeat=3))}

class TAPSPTaggedMolecule(AnnotatedTAPSNlaIIIMolecule):


    def __finalise__(self):
        AnnotatedTAPSNlaIIIMolecule.__finalise__(self)

        # Additional finalisation steps:
        conversions_count = 0
        forward_counts = 0
        rev_counts = 0
        conversion_locations = []
        mismatches = 0
        ambig_count = 0
        molecule_variants=set()

        # Obtain consensus call for the molecule
        consensus = self.get_consensus(allow_unsafe=True)

        context_obs = Counter()

        for read in self.iter_reads():
            for qpos, refpos, reference_base in read.get_aligned_pairs(with_seq=True, matches_only=True):

                location = (read.reference_name, refpos)

                #if (location[0], location[1]) in variant_locations:
                #    skipped+=1
                #    continue

                query = read.seq[qpos]
                context = self.reference.fetch(self.chromosome,refpos-1,refpos+2).upper()

                # Check if call is the same as the consensus call:
                if query!=consensus.get((read.reference_name,refpos),'X'):
                    #print(query, consensus.get((read.reference_name,refpos),'X'))
                    #print(read.reference_name, refpos,)
                    continue

                reference_base = reference_base.upper()
                if read.is_reverse: # forward, we sequence the other side
                    reference_base = complement(reference_base)
                    query = complement(query)
                    context=complement(context)

                if query!=reference_base:
                    mismatches += 1
                    molecule_variants.add(refpos)

                if (reference_base, query) in ( ('A','G'), ('G','A'), ('T','C'), ('C','T')):
                    conversion_locations.append(f'{location[0]}:{location[1]+1} {reference_base}>{query}' )
                    conversions_count+=1
                    context_obs[context_ids.get(context,'99')]+=1
                    if (reference_base, query) in ( ('A','G'), ('G','A') ):
                        forward_counts+=1
                    elif (reference_base, query) in (('T','C'), ('C','T')):
                        rev_counts+=1

        self.set_meta('mM',mismatches)
        self.set_meta('pP',conversions_count)
        self.set_meta('pR',rev_counts)
        self.set_meta('pF',forward_counts)
        self.set_meta('pE',','.join(conversion_locations))
        for context,obs in context_obs.most_common():
            self.set_meta(context,obs)

        strip_array_tags(self)
